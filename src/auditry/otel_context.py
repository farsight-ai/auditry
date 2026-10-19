"""
W3C trace context for log lines, outbound calls, and queue hops.

Every function here is a no-op until the service opts in to auditry tracing
(:func:`auditry.otel.build_tracer_provider`, which ``configure_tracing``
calls, or ``ObservabilityConfig(tracing=True)``). Installing the otel
extra is not enough: another dependency (FastAPI, Langfuse) can install the
OpenTelemetry SDK and its own tracer provider, and auditry must not stamp,
propagate, or add spans to that provider's traces unasked. Only the W3C
``traceparent``/``tracestate`` fields are carried: baggage can hold arbitrary
application values, and the propagation helpers must only ever move opaque
IDs between services.
"""

import importlib.util
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

# Every module auditry.otel imports beyond opentelemetry-api, which alone is
# not opt-in. A service with its own OpenTelemetry setup can have some of them
# (the SDK, the ASGI instrumentation) without the OTLP/HTTP exporter.
_OTEL_EXTRA_MODULES = (
    "opentelemetry.sdk",
    "opentelemetry.instrumentation.asgi",
    "opentelemetry.exporter.otlp.proto.http",
)


def otel_extra_installed() -> bool:
    """Whether the otel extra is installed, checked without importing it.
    find_spec imports the parent packages, so it raises ModuleNotFoundError
    rather than returning None when opentelemetry itself is absent."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in _OTEL_EXTRA_MODULES)
    except ModuleNotFoundError:
        return False


OTEL_AVAILABLE = otel_extra_installed()

if OTEL_AVAILABLE:
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Status, StatusCode
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
    "span_redaction_patterns",
    "trace_context",
    "tracing_enabled",
]

TRACE_CONTEXT_FIELDS = ("traceparent", "tracestate")

_propagator = TraceContextTextMapPropagator() if OTEL_AVAILABLE else None


@dataclass
class _TracingState:
    enabled: bool = False
    # ObservabilityConfig.additional_redaction_patterns from every
    # create_middleware call, read by RedactingSpanExporter at export time.
    span_redaction_patterns: frozenset[str] = frozenset()


_state = _TracingState()


def _enable_tracing() -> None:
    _state.enabled = True


def tracing_enabled() -> bool:
    """Whether the service opted in to auditry tracing and the extra is installed."""
    return OTEL_AVAILABLE and _state.enabled


def _add_span_redaction_patterns(patterns: Optional[Iterable[str]]) -> None:
    _state.span_redaction_patterns |= frozenset(patterns or ())


def _register_config(additional_redaction_patterns: Optional[Iterable[str]], tracing: bool) -> None:
    """Called by create_middleware with the ObservabilityConfig's settings."""
    _add_span_redaction_patterns(additional_redaction_patterns)
    if tracing:
        _enable_tracing()


def span_redaction_patterns() -> frozenset[str]:
    """The ``additional_redaction_patterns`` of every ``ObservabilityConfig``
    passed to ``create_middleware`` in this process."""
    return _state.span_redaction_patterns


def current_trace_ids() -> Optional[tuple[str, str]]:
    """``(trace_id, span_id)`` as lowercase hex for the active span, or ``None``
    when tracing is off or there is no valid span."""
    if not tracing_enabled():
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
    tracing is off or there is no valid span."""
    carrier: dict[str, str] = {}
    if tracing_enabled():
        _propagator.inject(carrier)  # type: ignore[union-attr]
    return carrier


def attach_trace_context(carrier: Mapping[str, str]) -> Optional[object]:
    """
    Make the trace carried by ``carrier`` the current context, so log lines
    and outbound calls continue it instead of starting a new trace.

    Returns a token for :func:`detach_trace_context`, or ``None`` when tracing
    is off or the carrier has no valid ``traceparent`` (the current context is
    left as is, so an in-flight trace is never replaced by nothing).
    """
    if not tracing_enabled() or not carrier.get("traceparent"):
        return None
    # extract() starts from an empty Context, so a malformed traceparent
    # yields a context with no span at all.
    extracted = _propagator.extract(carrier)  # type: ignore[union-attr]
    if not otel_trace.get_current_span(extracted).get_span_context().is_valid:
        return None
    return otel_context.attach(extracted)


def detach_trace_context(token: Optional[object]) -> None:
    """Restore the context that was current before :func:`attach_trace_context`."""
    if token is not None:
        otel_context.detach(token)  # type: ignore[arg-type]


@contextmanager
def consumer_span(
    name: str,
    correlation_id: Optional[str] = None,
    tracer_provider: Optional[Any] = None,
) -> Iterator[None]:
    """
    A CONSUMER span around one unit of worker work (a message, a job), child
    of the current context, so the work appears in the producer's trace.

    The span comes from ``tracer_provider``, else the global provider. An
    exception escaping the block sets the span's status to ERROR with its
    ``error.type`` and is re-raised; the exception itself is never recorded,
    because a provider auditry did not build exports its message and stack
    trace unredacted. Without a configured SDK the span is non-recording and
    only carries the parent's IDs, so log lines keep the producer's trace.
    """
    if not tracing_enabled():
        yield
        return
    tracer = otel_trace.get_tracer("auditry", tracer_provider=tracer_provider)
    with tracer.start_as_current_span(
        name,
        kind=otel_trace.SpanKind.CONSUMER,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        if correlation_id and span.is_recording():
            span.set_attribute("correlation_id", correlation_id)
        try:
            yield
        except Exception as error:
            span.set_attribute("error.type", type(error).__qualname__)
            span.set_status(Status(StatusCode.ERROR))
            raise


def annotate_server_span(method: str, route: Optional[str], correlation_id: Optional[str]) -> None:
    """
    Name the active server span by its route template and tag it with the
    correlation ID.

    Span names default to the HTTP method alone (see
    ``auditry.otel.middleware``), because the raw path can carry identifiers
    and the template is only known once the framework has routed the request.
    """
    if not tracing_enabled():
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
