"""
Structured logging configuration using structlog.

This module provides JSON-formatted logging with production-safe defaults:

- Every line carries a consistent root schema: ``timestamp``, ``level``,
  ``service``, ``version``, ``environment``, ``correlation_id``,
  ``message`` (plus event-specific fields).
- Single-line JSON, safe for line-oriented aggregators (CloudWatch et al).
- Stack traces and exception messages are NOT serialized onto the standard
  stream — they frequently interpolate user-supplied content. Errors carry
  ``error_type`` (the exception class name) + the correlation ID. Full
  tracebacks can be routed to a gated destination via
  :func:`set_trace_handler` (e.g. an encrypted, access-controlled log
  group), or enabled inline for local development with
  ``AUDITRY_FULL_TRACEBACKS=true``.
- The correlation ID is attached to every log line automatically (from the
  ASGI middleware context or a worker binding — see ``auditry.propagation``).
"""

import logging
import os
import sys
from typing import Any, Callable, Dict, MutableMapping, Optional

import structlog
from asgi_correlation_id import correlation_id

# ---------------------------------------------------------------------------
# Service context — service/version/environment stamped on every log line, so
# lines stay self-describing when several services share a log destination.
# ---------------------------------------------------------------------------

_service_context: Dict[str, str] = {}

# Handler for full tracebacks. Signature: (error_type, traceback_text,
# event_dict) -> None. Register with set_trace_handler(). The handler is
# responsible for routing to a gated surface — typically a dedicated logger
# whose stream ships to an encrypted, access-controlled destination — and
# MUST NOT write back to the standard stdout stream.
_trace_handler: Optional[Callable[[str, str, Dict[str, Any]], None]] = None

_FULL_TRACEBACKS_ENV = "AUDITRY_FULL_TRACEBACKS"


def set_trace_handler(handler: Optional[Callable[[str, str, Dict[str, Any]], None]]) -> None:
    """
    Register a handler that receives full exception tracebacks.

    By default auditry never serializes tracebacks or exception messages onto
    the standard log stream, because they can interpolate sensitive
    user-supplied content (request payloads, document text, PII). Services
    that need full traces must route them to a gated destination:

    ```python
    from auditry import set_trace_handler

    def route_to_secure_log(error_type, traceback_text, event_dict):
        # e.g. a dedicated stdlib logger whose output ships to an
        # encrypted, access-controlled log group — never back to stdout.
        secure_logger.error(
            "%s correlation_id=%s\\n%s",
            error_type, event_dict.get("correlation_id"), traceback_text,
        )

    set_trace_handler(route_to_secure_log)
    ```

    The ``event_dict`` snapshot follows the root schema — the log text is
    under ``message``, alongside ``correlation_id``, ``service``, etc.

    Pass ``None`` to remove the handler.
    """
    global _trace_handler
    _trace_handler = handler


def get_trace_handler() -> Optional[Callable[[str, str, Dict[str, Any]], None]]:
    """Return the currently registered trace handler, if any."""
    return _trace_handler


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------

def _add_service_context(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Stamp service/version/environment onto every line."""
    for key, value in _service_context.items():
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
    Replacement for structlog's format_exc_info.

    Extracts the exception class name as ``error_type`` and drops the
    traceback from the standard stream (tracebacks and exception messages
    can carry sensitive user content). The full traceback is handed to the
    registered trace handler (see :func:`set_trace_handler`) or, for local
    development only, inlined when ``AUDITRY_FULL_TRACEBACKS=true``.
    """
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict

    import traceback as _tb

    if exc_info is True:
        exc_info = sys.exc_info()
    if not (isinstance(exc_info, tuple) and exc_info[0] is not None):
        return event_dict

    error_type = exc_info[0].__name__
    event_dict.setdefault("error_type", error_type)

    handler = _trace_handler
    wants_full = os.environ.get(_FULL_TRACEBACKS_ENV, "").lower() in ("1", "true", "yes")
    if handler is not None or wants_full:
        traceback_text = "".join(_tb.format_exception(*exc_info))
        if handler is not None:
            try:
                handler(error_type, traceback_text, dict(event_dict))
            except Exception:
                # A failing trace handler must never break application logging.
                event_dict["trace_handler_error"] = True
        if wants_full:
            # Dev-only escape hatch: kept single-line for the standard stream.
            event_dict["exception"] = traceback_text.replace("\n", " | ")
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
) -> None:
    """
    Configure application-wide structured logging using structlog.

    Sets up single-line JSON logging on stdout carrying the standard root
    schema and the correlation ID on every line. Should be called once at
    application startup — including worker processes (see
    ``auditry.propagation`` for binding correlation IDs outside ASGI).

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            DEBUG should be off in production.
        service: Service name; falls back to the SERVICE_NAME env var.
        version: Service version; falls back to SERVICE_VERSION.
        environment: Deployment environment; falls back to ENVIRONMENT.
    """
    _service_context.clear()
    resolved = {
        "service": service or os.environ.get("SERVICE_NAME"),
        "version": version or os.environ.get("SERVICE_VERSION"),
        "environment": environment or os.environ.get("ENVIRONMENT"),
    }
    _service_context.update({k: v for k, v in resolved.items() if v})

    # One processor chain, applied to BOTH structlog-originated events and
    # foreign stdlib records (uvicorn, boto3, any library calling
    # logging.getLogger(...)), so every line on stdout carries the same
    # JSON schema. Rendering happens exactly once, in the formatter.
    shared_processors = [
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
        # "message" is the schema key — renamed BEFORE _error_type_only so the
        # trace handler's event_dict snapshot matches the documented schema
        _rename_event_to_message,
        # error_type only; full traces go to the gated handler
        _error_type_only,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand the event dict to the stdlib formatter below for rendering
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # Use standard library logging
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Foreign stdlib records run the same chain via foreign_pre_chain, so
    # they get timestamps, service context, correlation IDs, and the
    # error_type-only exception treatment — not just "%(message)s".
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
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
