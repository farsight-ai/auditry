"""
W3C trace context for log lines, outbound calls, and queue hops.

Every function here is a no-op unless the ``otel`` extra is installed, so
services that have not opted in behave exactly as before, even when another
dependency (FastAPI does) pulls in ``opentelemetry-api``. Only the W3C
``traceparent``/``tracestate`` fields are carried: baggage can hold arbitrary
application values, and the propagation helpers must only ever move opaque
IDs between services.
"""

import importlib.util
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Optional

# Modules only the otel extra installs; opentelemetry-api alone is not opt-in.
_OTEL_EXTRA_MODULES = ("opentelemetry.sdk", "opentelemetry.instrumentation.asgi")


def otel_extra_installed() -> bool:
    """Whether the ``otel`` extra is installed, checked without importing it."""
    # find_spec imports the parent packages, so it raises rather than
    # returning None when opentelemetry itself is absent.
    try:
        return all(importlib.util.find_spec(name) is not None for name in _OTEL_EXTRA_MODULES)
    except ModuleNotFoundError:
        return False


OTEL_AVAILABLE = otel_extra_installed()

if OTEL_AVAILABLE:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

__all__ = [
    "OTEL_AVAILABLE",
    "TRACE_CONTEXT_FIELDS",
    "annotate_server_span",
    "attach_trace_context",
    "consumer_span",
    "current_trace_ids",
    "detach_trace_context",
    "otel_extra_installed",
    "trace_context",
]

TRACE_CONTEXT_FIELDS = ("traceparent", "tracestate")

_propagator = TraceContextTextMapPropagator() if OTEL_AVAILABLE else None


def _tracer() -> Any:
    return otel_trace.get_tracer("auditry")


def current_trace_ids() -> Optional[tuple[str, str]]:
    """``(trace_id, span_id)`` as lowercase hex for the active span, or ``None``
    when there is no valid span (no SDK configured, or outside any trace)."""
    if not OTEL_AVAILABLE:
        return None
    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return (
        otel_trace.format_trace_id(span_context.trace_id),
        otel_trace.format_span_id(span_context.span_id),
    )


def trace_context() -> dict[str, str]:
    """The active span's ``traceparent`` (and ``tracestate`` when set), ready to
    merge into outbound headers, message attributes, or job kwargs. Empty when
    there is no valid span."""
    carrier: dict[str, str] = {}
    if _propagator is not None:
        _propagator.inject(carrier)
    return carrier


def attach_trace_context(carrier: Mapping[str, str]) -> Optional[object]:
    """
    Make the trace carried by ``carrier`` the current context, so log lines
    and outbound calls continue it instead of starting a new trace.

    Returns a token for :func:`detach_trace_context`, or ``None`` when the
    carrier has no valid ``traceparent`` (the current context is left as is,
    so an in-flight trace is never replaced by nothing).
    """
    if _propagator is None or not carrier.get("traceparent"):
        return None
    # extract() starts from an empty Context, so a malformed traceparent
    # yields a context with no span at all.
    extracted = _propagator.extract(carrier)
    if not otel_trace.get_current_span(extracted).get_span_context().is_valid:
        return None
    return otel_context.attach(extracted)


def detach_trace_context(token: Optional[object]) -> None:
    """Restore the context that was current before :func:`attach_trace_context`."""
    if token is not None:
        otel_context.detach(token)  # type: ignore[arg-type]


@contextmanager
def consumer_span(name: str, correlation_id: Optional[str] = None) -> Iterator[None]:
    """
    A CONSUMER span around one unit of worker work (a message, a job), child
    of the current context, so the work appears in the producer's trace.

    An exception escaping the block marks the span as an error and is
    re-raised. Without a configured SDK the span is non-recording and only
    carries the parent's IDs, so log lines keep the producer's trace.
    """
    if not OTEL_AVAILABLE:
        yield
        return
    with _tracer().start_as_current_span(name, kind=otel_trace.SpanKind.CONSUMER) as span:
        if correlation_id and span.is_recording():
            span.set_attribute("correlation_id", correlation_id)
        yield


def annotate_server_span(method: str, route: Optional[str], correlation_id: Optional[str]) -> None:
    """
    Name the active server span by its route template and tag it with the
    correlation ID.

    Span names default to the HTTP method alone (see
    ``auditry.otel.middleware``), because the raw path can carry identifiers
    and the template is only known once the framework has routed the request.
    """
    if not OTEL_AVAILABLE:
        return
    span = otel_trace.get_current_span()
    if not span.is_recording():
        return
    attributes: dict[str, Any] = {}
    if route:
        span.update_name(f"{method} {route}")
        attributes["http.route"] = route
    if correlation_id:
        attributes["correlation_id"] = correlation_id
    span.set_attributes(attributes)
