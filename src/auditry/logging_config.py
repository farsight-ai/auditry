"""
Structured logging configuration using structlog.

This module provides JSON-formatted logging with production-safe defaults:

- Every line carries a consistent root schema: ``timestamp``, ``level``,
  ``service``, ``version``, ``environment``, ``correlation_id``,
  ``message`` (plus event-specific fields).
- Single-line JSON, safe for line-oriented aggregators (CloudWatch et al).
- auditry's own records (the middleware's request/response lines and any
  logger from :func:`get_logger`) do NOT serialize stack traces or exception
  messages onto the standard stream — they frequently interpolate
  user-supplied content. Those errors carry ``error_type`` (the exception
  class name) + the correlation ID. Full tracebacks can be routed to a gated
  destination via :func:`set_trace_handler` (e.g. an encrypted,
  access-controlled log group), or enabled inline for local development
  with ``AUDITRY_FULL_TRACEBACKS=true``.
- Foreign stdlib records (``logging.getLogger(...)`` in application or vendor
  code) share the root schema but keep their tracebacks: auditry's error
  discipline is scoped to auditry's records, not to every ``except`` block
  in the process.
- The correlation ID is attached to every log line automatically (from the
  ASGI middleware context or a worker binding — see ``auditry.propagation``).
- Instrumentation never fails the unit of work it measures. In production a
  bad metric dimension or a broken trace handler degrades to "drop and
  warn". In a *known* non-production environment the same failures raise
  (**strict mode**, see :func:`is_strict`), so they are caught in tests,
  local runs, dev, and sandbox — long before production.
"""

import logging
import os
import sys
from types import TracebackType
from typing import Any, Callable, Dict, MutableMapping, Optional, Tuple, Type

import structlog
from asgi_correlation_id import correlation_id

# ---------------------------------------------------------------------------
# Service context — service/version/environment stamped on every log line, so
# lines stay self-describing when several services share a log destination.
# ---------------------------------------------------------------------------

_service_context: Dict[str, str] = {}

# The service identity from ObservabilityConfig.service_name, seeded by
# create_middleware(). It is the source of truth for ``service`` whenever
# middleware is present; ``configure_logging(service=)`` / SERVICE_NAME are
# the fallback for processes without middleware (workers, scripts). Kept
# separate from _service_context so it survives configure_logging() being
# called in either order relative to create_middleware().
_config_service: Optional[str] = None

# Resolved once by configure_logging(), not per record.
_full_tracebacks: bool = False
_FULL_TRACEBACKS_ENV = "AUDITRY_FULL_TRACEBACKS"

# ---------------------------------------------------------------------------
# Strict mode — the loud version of instrumentation, for non-production only.
# ---------------------------------------------------------------------------
# Production rule: instrumentation never fails the unit of work it measures.
# A bad metric dimension, a broken trace handler — in production these
# degrade to "drop and warn". In non-production the same failures raise, so
# they are caught in tests, local runs, dev, and sandbox, long before
# production. configure_logging() resolves the policy once, from (highest
# wins): an explicit strict= argument, the AUDITRY_STRICT env var, or the
# environment name.
#
# Only a KNOWN non-production name turns strict on. Anything unrecognized —
# including no environment at all — is treated as production. Deriving the
# other way round ("strict unless it says prod") would turn strict on in a
# dedicated customer account whose stage name carries no hint.
NON_PRODUCTION_ENVIRONMENTS = frozenset({
    "local", "dev", "development", "sandbox", "plat-sandbox",
    "test", "testing", "ci", "qa", "staging", "stage",
})
NON_PRODUCTION_PREFIXES = ("local-", "dev-")
_STRICT_ENV = "AUDITRY_STRICT"
_strict: bool = False


def is_non_production_environment(environment: Optional[str]) -> bool:
    """True only for a *known* non-production environment name; unknown or
    unset is treated as production."""
    if not environment:
        return False
    name = environment.strip().lower()
    return name in NON_PRODUCTION_ENVIRONMENTS or name.startswith(NON_PRODUCTION_PREFIXES)


def is_strict() -> bool:
    """Whether instrumentation failures raise (strict, non-production) or
    degrade to drop-and-warn (production). Resolved by :func:`configure_logging`;
    False until it runs."""
    return _strict


def _set_strict(value: bool) -> None:
    """Set the policy directly (tests)."""
    global _strict
    _strict = value


def _resolve_strict(explicit: Optional[bool], environment: Optional[str]) -> bool:
    if explicit is not None:
        return explicit
    flag = os.environ.get(_STRICT_ENV, "").strip().lower()
    if flag:
        return flag in ("1", "true", "yes")
    return is_non_production_environment(environment)

ExcInfo = Tuple[Type[BaseException], BaseException, Optional[TracebackType]]
TraceHandler = Callable[[str, ExcInfo, Dict[str, Any]], None]

# Handler for full tracebacks. Signature: (error_type, exc_info, event_dict)
# -> None. Register with set_trace_handler(). The handler is responsible for
# routing to a gated surface — typically a dedicated logger whose stream
# ships to an encrypted, access-controlled destination, or an error tracker
# — and MUST NOT write back to the standard stdout stream.
_trace_handler: Optional[TraceHandler] = None


def set_trace_handler(handler: Optional[TraceHandler]) -> None:
    """
    Register a handler that receives full exception details.

    By default auditry never serializes tracebacks or exception messages onto
    the standard log stream, because they can interpolate sensitive
    user-supplied content (request payloads, document text, PII). Services
    that need full traces must route them to a gated destination.

    The handler receives ``(error_type, exc_info, event_dict)``: the exception
    class name, the live ``(type, value, traceback)`` tuple, and a snapshot of
    the log line's fields (root schema — the log text is under ``message``,
    alongside ``correlation_id``, ``service``, etc.). Passing the tuple rather
    than rendered text means error trackers work directly:

    ```python
    from auditry import set_trace_handler

    # An error tracker gets the real exception object:
    set_trace_handler(
        lambda error_type, exc_info, event_dict: sentry_sdk.capture_exception(exc_info[1])
    )

    # A gated log destination renders text itself:
    import traceback

    def route_to_secure_log(error_type, exc_info, event_dict):
        # e.g. a dedicated stdlib logger whose output ships to an
        # encrypted, access-controlled log group — never back to stdout.
        secure_logger.error(
            "%s correlation_id=%s\\n%s",
            error_type,
            event_dict.get("correlation_id"),
            "".join(traceback.format_exception(*exc_info)),
        )

    set_trace_handler(route_to_secure_log)
    ```

    Pass ``None`` to remove the handler.
    """
    global _trace_handler
    _trace_handler = handler


def get_trace_handler() -> Optional[TraceHandler]:
    """Return the currently registered trace handler, if any."""
    return _trace_handler


def _set_config_service(service_name: Optional[str]) -> None:
    """Seed the service identity from ObservabilityConfig (called by
    create_middleware). Overrides configure_logging(service=) / SERVICE_NAME
    so one process never emits two different ``service`` values. ``None``
    clears it (tests)."""
    global _config_service
    _config_service = service_name


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------

def _add_service_context(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Stamp service/version/environment onto every line.

    ``service`` comes from ObservabilityConfig.service_name when middleware
    has been created, else from configure_logging(service=) / SERVICE_NAME.
    """
    service = _config_service or _service_context.get("service")
    if service:
        event_dict.setdefault("service", service)
    for key in ("version", "environment"):
        value = _service_context.get(key)
        if value:
            event_dict.setdefault(key, value)
    return event_dict


def _add_correlation_id(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Every log line carries the correlation ID when one is bound.

    ``correlation_id`` is an *optional* field of the root schema: it is
    present whenever a context is bound (ASGI middleware, or
    ``auditry.propagation`` in workers) and absent otherwise — e.g. a log
    line emitted at import time or from a startup hook. Consumers must not
    assume the field exists on every line. A per-line random fallback would
    be worse than absence: each line would carry a *different* ID, which
    falsely implies correlation where there is none.
    """
    if "correlation_id" not in event_dict:
        cid = correlation_id.get()
        if cid:
            event_dict["correlation_id"] = cid
    return event_dict


def _error_type_only(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """
    Replacement for structlog's format_exc_info, for auditry's own records.

    Extracts the exception class name as ``error_type`` and drops the
    traceback from the standard stream (tracebacks and exception messages
    can carry sensitive user content). The live exc_info is handed to the
    registered trace handler (see :func:`set_trace_handler`) or, for local
    development only, the traceback is inlined when
    ``AUDITRY_FULL_TRACEBACKS=true``.
    """
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict

    if exc_info is True:
        exc_info = sys.exc_info()
    if not (isinstance(exc_info, tuple) and exc_info[0] is not None):
        return event_dict

    error_type = exc_info[0].__name__
    event_dict.setdefault("error_type", error_type)

    handler = _trace_handler
    if handler is not None:
        try:
            handler(error_type, exc_info, dict(event_dict))
        except Exception:
            if _strict:
                # Non-production: a broken trace handler is a bug to fix now.
                raise
            # Production: a failing trace handler must never break
            # application logging.
            event_dict["trace_handler_error"] = True
    if _full_tracebacks:
        import traceback as _tb

        # Dev-only escape hatch. The traceback is passed through unmodified —
        # the JSON renderer escapes its newlines, so the stream line stays
        # single-line while the decoded value stays a real, tool-parseable
        # traceback.
        event_dict["exception"] = "".join(_tb.format_exception(*exc_info))
    return event_dict


def _rename_event_to_message(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """The schema field is ``message``, not structlog's ``event``."""
    if "event" in event_dict and "message" not in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(
    level: str = "INFO",
    service: Optional[str] = None,
    version: Optional[str] = None,
    environment: Optional[str] = None,
    strict: Optional[bool] = None,
) -> None:
    """
    Configure application-wide structured logging using structlog.

    Sets up single-line JSON logging on stdout carrying the standard root
    schema and the correlation ID on every line. Should be called once at
    application startup — including worker processes (see
    ``auditry.propagation`` for binding correlation IDs outside ASGI).

    Also resolves **strict mode** — whether instrumentation failures raise
    (non-production) or degrade to drop-and-warn (production). Resolution,
    highest wins: ``strict=`` here, the ``AUDITRY_STRICT`` env var (``1``/``0``),
    then the environment: strict only for a *known* non-production name
    (``local``, ``dev``, ``sandbox``, ``test``, ``staging``, …); production,
    anything unrecognized, and no environment at all are production-safe.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            DEBUG should be off in production.
        service: Service name; falls back to the SERVICE_NAME env var. When
            middleware is created, ObservabilityConfig.service_name takes
            precedence over both.
        version: Service version; falls back to SERVICE_VERSION.
        environment: Deployment environment; falls back to ENVIRONMENT.
        strict: Force strict mode on or off. Leave unset to derive it from
            the environment (see above).
    """
    global _full_tracebacks, _strict

    _service_context.clear()
    resolved = {
        "service": service or os.environ.get("SERVICE_NAME"),
        "version": version or os.environ.get("SERVICE_VERSION"),
        "environment": environment or os.environ.get("ENVIRONMENT"),
    }
    _service_context.update({k: v for k, v in resolved.items() if v})
    _full_tracebacks = os.environ.get(_FULL_TRACEBACKS_ENV, "").lower() in ("1", "true", "yes")
    _strict = _resolve_strict(strict, resolved["environment"])

    # The schema processors are shared by structlog-originated events and
    # foreign stdlib records (uvicorn, boto3, any library calling
    # logging.getLogger(...)), so every line on stdout carries the same JSON
    # root schema. Rendering happens exactly once, in the formatter.
    schema_processors = [
        # Add log level to event dict
        structlog.stdlib.add_log_level,
        # Add timestamp in ISO format (UTC)
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Merge structlog contextvars (correlation_id, user_id, ...)
        structlog.contextvars.merge_contextvars,
        # service / version / environment on every line
        _add_service_context,
        # correlation ID on every line (when a context is bound)
        _add_correlation_id,
        # "message" is the schema key — renamed BEFORE the exception
        # processors so the trace handler's event_dict snapshot matches the
        # documented schema
        _rename_event_to_message,
    ]

    structlog.configure(
        processors=[
            *schema_processors,
            # auditry's own records: error_type only; full traces go to the
            # gated handler
            _error_type_only,
            # Hand the event dict to the stdlib formatter below for rendering
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # Use standard library logging
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Foreign stdlib records get the same root schema but KEEP their
    # tracebacks (structlog's stock format_exc_info renders them into the
    # ``exception`` field, JSON-escaped, still one line). The error
    # discipline above is scoped to auditry's own records: stripping the
    # stack trace from every logger.exception() call in application and
    # vendor code is not auditry's call to make.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*schema_processors, structlog.processors.format_exc_info],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Render as single-line JSON
            structlog.processors.JSONRenderer(),
        ],
    )

    # Configure standard library logging (stdout; container agents route it)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    for existing in root_logger.handlers[:]:
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper()))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a structlog logger instance.

    This logger automatically includes the root schema fields and the
    correlation ID, and outputs single-line structured JSON.

    Args:
        name: Logger name (typically __name__ of the module)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
