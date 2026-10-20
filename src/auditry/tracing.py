"""
OpenTelemetry API glue for the core modules.

Every function is a no-op when the OpenTelemetry API (``opentelemetry-api``)
is not importable, so ``pip install auditry`` never needs OpenTelemetry. The
API alone turns them on, whether it came with the ``otel`` extra or with
another package, such as Langfuse, that depends on it. Propagation uses W3C Trace
Context only, through an explicit propagator rather than the global one: the
behavior does not depend on ``OTEL_PROPAGATORS``, and ``baggage`` (arbitrary
application data) is never forwarded.
"""

import functools
import importlib.util
from collections.abc import Callable, Mapping
from contextvars import Token
from typing import TYPE_CHECKING, Literal, ParamSpec, TypeVar

import structlog

from .correlation import CORRELATION_ID_FIELD

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError:
    otel_context = None  # type: ignore[assignment]
    otel_trace = None  # type: ignore[assignment]
    TraceContextTextMapPropagator = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from opentelemetry.context import Context

__all__ = [
    "TRACE_LOG_FIELDS",
    "CORRELATION_ID_ATTRIBUTE",
    "OTEL_LOGGER_NAME",
    "drop_or_raise",
    "current_trace_fields",
    "inject_trace_context",
    "context_from_carrier",
    "current_context",
    "attach_context",
    "detach_context",
    "set_span_correlation_id",
    "require_otel",
]

TRACE_ID_FIELD = "trace_id"
SPAN_ID_FIELD = "span_id"
TRACE_LOG_FIELDS = (TRACE_ID_FIELD, SPAN_ID_FIELD)

CORRELATION_ID_ATTRIBUTE = CORRELATION_ID_FIELD

# Tracing failure warnings go through this structlog logger.
OTEL_LOGGER_NAME = "auditry.otel"

# The per-message child spans OpenTelemetry's ASGI middleware would add under
# every server span; auditry's server instrumentation leaves them out.
EXCLUDED_ASGI_SPANS: tuple[Literal["receive", "send"], ...] = ("receive", "send")

_propagator = TraceContextTextMapPropagator() if TraceContextTextMapPropagator is not None else None

# (operation, exception type) pairs already warned about in this process.
_warned: set[tuple[str, str]] = set()

P = ParamSpec("P")
R = TypeVar("R")


def drop_or_raise(
    operation: str, exc: Exception, warned: set[tuple[str, str]] | None = None
) -> None:
    """The instrumentation failure policy for tracing code. Strict mode
    raises ``exc``. Production drops the signal and logs one warning per
    operation and exception type, per process or per ``warned`` set. The
    warning carries the exception type only: messages can carry user content.
    """
    # Deferred: logging_config imports this module. Only the failure path
    # pays for the lookup.
    from .logging_config import is_strict  # noqa: PLC0415 -- import cycle, see above

    if is_strict():
        raise exc
    seen = _warned if warned is None else warned
    marker = (operation, type(exc).__name__)
    if marker in seen:
        return
    # Marked before logging: the warning itself passes through the log
    # processor that adds trace fields, which may be what failed.
    seen.add(marker)
    structlog.get_logger(OTEL_LOGGER_NAME).warning(
        "tracing signal dropped", operation=operation, error_type=type(exc).__name__
    )


def _reset_warnings() -> None:
    """Forget which failures were warned about (tests)."""
    _warned.clear()


def _never_raises(func: Callable[P, R]) -> Callable[P, R | None]:
    """Applies :func:`drop_or_raise` to any exception ``func`` raises; the
    dropped call returns None."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            drop_or_raise(func.__name__, exc)
            return None

    return wrapper


def current_trace_fields() -> dict[str, str]:
    """``trace_id`` / ``span_id`` (lowercase W3C hex) of the current span, or
    ``{}`` outside a valid span context."""
    if otel_trace is None:
        return {}
    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        TRACE_ID_FIELD: otel_trace.format_trace_id(span_context.trace_id),
        SPAN_ID_FIELD: otel_trace.format_span_id(span_context.span_id),
    }


@_never_raises
def inject_trace_context(carrier: dict[str, str]) -> None:
    """Write ``traceparent`` (and ``tracestate`` when non-empty) for the
    current span into ``carrier``. Writes nothing outside a valid span."""
    if _propagator is not None:
        _propagator.inject(carrier)


@_never_raises
def context_from_carrier(carrier: Mapping[str, str]) -> "Context | None":
    """The context carried by ``carrier``'s ``traceparent``, or ``None`` when
    it holds no valid one."""
    if _propagator is None or otel_trace is None:
        return None
    ctx = _propagator.extract(carrier)
    if not otel_trace.get_current_span(ctx).get_span_context().is_valid:
        return None
    return ctx


@_never_raises
def current_context() -> "Context | None":
    """The current OpenTelemetry context, or ``None`` without the extra."""
    if otel_context is None:
        return None
    return otel_context.get_current()


@_never_raises
def attach_context(ctx: "Context | None") -> "Token[Context] | None":
    """Make ``ctx`` current; returns the token for :func:`detach_context`.
    ``None`` attaches nothing."""
    if ctx is None or otel_context is None:
        return None
    return otel_context.attach(ctx)


@_never_raises
def detach_context(token: "Token[Context] | None") -> None:
    """Restore the context that was current before :func:`attach_context`.
    ``None`` detaches nothing."""
    if token is not None and otel_context is not None:
        otel_context.detach(token)


@_never_raises
def set_span_correlation_id(value: str | None) -> None:
    """Record the correlation ID on the current span, if it is recording."""
    if not value or otel_trace is None:
        return
    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute(CORRELATION_ID_ATTRIBUTE, value)


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        # find_spec imports the parent packages, and raises when one is missing.
        return False


def require_otel(feature: str, *modules: str) -> None:
    """Raise ImportError naming the ``otel`` extra when the OpenTelemetry API,
    or any of ``modules``, is not installed."""
    if otel_trace is None or not all(_importable(module) for module in modules):
        raise ImportError(f"{feature} requires the 'otel' extra: pip install 'auditry[otel]'")
