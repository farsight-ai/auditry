"""Opt-in OpenTelemetry server spans for Quart. Requires the ``otel`` extra.

There is no official Quart instrumentation, so this wraps the ASGI app in
OpenTelemetry's generic ASGI middleware and names each span by its route.
"""

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import TracerProvider, get_current_span
from quart import Quart, has_websocket_context, request, websocket

from ..models import ObservabilityConfig
from ..otel.asgi import TracedUnlessExcluded
from ..span_scrubbing import HTTP_ROUTE_KEY
from ..tracing import EXCLUDED_ASGI_SPANS, drop_or_raise
from .middleware import CONNECTION_STARTED_SIGNALS

__all__ = ["instrument_quart"]


def instrument_quart(
    app: Quart, config: ObservabilityConfig, tracer_provider: TracerProvider | None = None
) -> None:
    """One server span per request, named by route rule, skipping
    ``config.excluded_paths`` exactly as request logging does, method-specific
    patterns included. No per-``receive``/``send`` child spans.

    Call it after every other ``app.asgi_app`` wrapper, so the span is
    active in all of them.
    """
    if _already_traced(app.asgi_app):
        return
    traced = OpenTelemetryMiddleware(
        app.asgi_app,
        exclude_spans=list(EXCLUDED_ASGI_SPANS),
        tracer_provider=tracer_provider,
    )
    app.asgi_app = TracedUnlessExcluded(  # type: ignore[assignment]
        app.asgi_app, traced, config.excluded_paths
    )
    for signal in CONNECTION_STARTED_SIGNALS:
        signal.connect(_name_span_by_route, sender=app)


def _already_traced(asgi_app: object) -> bool:
    """Whether OpenTelemetry's middleware already wraps the app anywhere in its
    chain of ASGI wrappers, followed through each one's ``app``. By the time
    ``create_middleware`` instruments, QuartMiddleware's CorrelationIdMiddleware
    sits outside a service's own instrumentation."""
    seen: set[int] = set()
    layer: object = asgi_app
    while layer is not None and id(layer) not in seen:
        if isinstance(layer, OpenTelemetryMiddleware | TracedUnlessExcluded):
            return True
        seen.add(id(layer))
        layer = getattr(layer, "app", None)
    return False


async def _name_span_by_route(sender: Quart, **extra: object) -> None:
    """The generic ASGI middleware names spans by raw path; the route rule
    is known once Quart has matched the request or websocket."""
    try:
        connection = websocket if has_websocket_context() else request
        rule = connection.url_rule
        span = get_current_span()
        if rule is not None and span.is_recording():
            span.update_name(f"{connection.method} {rule.rule}")
            span.set_attribute(HTTP_ROUTE_KEY, rule.rule)
    except Exception as exc:
        drop_or_raise("quart_span_name", exc)
