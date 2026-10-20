"""Opt-in OpenTelemetry server spans for Quart. Requires the ``otel`` extra.

There is no official Quart instrumentation, so this wraps the ASGI app in
OpenTelemetry's generic ASGI middleware and names each span by its route.
"""

from hypercorn.typing import ASGIFramework, ASGIReceiveCallable, ASGISendCallable, Scope
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import TracerProvider, get_current_span
from quart import Quart, has_websocket_context, request, websocket

from ..models import ObservabilityConfig
from ..path_matcher import should_exclude_path
from ..span_scrubbing import HTTP_ROUTE_KEY
from ..tracing import EXCLUDED_ASGI_SPANS, drop_or_raise
from .middleware import CONNECTION_STARTED_SIGNALS

__all__ = ["instrument_quart"]


class _TracedUnlessExcluded:
    """Sends a request to the OpenTelemetry middleware unless
    ``excluded_paths`` matches it, in which case it bypasses tracing.

    The decision uses the ASGI path and method, as request logging does.
    OpenTelemetry's own ``excluded_urls`` would match a URL built from the
    client's Host header, which a client can craft to skip its span."""

    def __init__(
        self,
        app: ASGIFramework,
        traced: OpenTelemetryMiddleware,
        excluded_paths: list[str] | dict[str, list[str]] | None,
    ) -> None:
        self.app = app
        self.traced = traced
        self.excluded_paths = excluded_paths

    async def __call__(
        self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        # A websocket scope has no method; its handshake is a GET.
        excluded = scope["type"] in ("http", "websocket") and should_exclude_path(
            scope["path"], scope.get("method", "GET"), self.excluded_paths
        )
        await (self.app if excluded else self.traced)(scope, receive, send)


def instrument_quart(
    app: Quart, config: ObservabilityConfig, tracer_provider: TracerProvider | None = None
) -> None:
    """One server span per request, named by route rule, skipping
    ``config.excluded_paths`` exactly as request logging does, method-specific
    patterns included. No per-``receive``/``send`` child spans.

    Call it after every other ``app.asgi_app`` wrapper, so the span is
    active in all of them.
    """
    if isinstance(app.asgi_app, OpenTelemetryMiddleware | _TracedUnlessExcluded):
        return
    traced = OpenTelemetryMiddleware(
        app.asgi_app,
        exclude_spans=list(EXCLUDED_ASGI_SPANS),
        tracer_provider=tracer_provider,
    )
    app.asgi_app = _TracedUnlessExcluded(  # type: ignore[assignment]
        app.asgi_app, traced, config.excluded_paths
    )
    for signal in CONNECTION_STARTED_SIGNALS:
        signal.connect(_name_span_by_route, sender=app)


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
