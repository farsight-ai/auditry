"""Opt-in OpenTelemetry server spans for Quart. Requires the ``otel`` extra.

There is no official Quart instrumentation, so this wraps the ASGI app in
OpenTelemetry's generic ASGI middleware. QuartMiddleware names each span by
its route, here or in a service's own instrumentation.
"""

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import TracerProvider
from quart import Quart

from ..models import ObservabilityConfig
from ..otel.asgi import TracedUnlessExcluded
from ..tracing import EXCLUDED_ASGI_SPANS

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
