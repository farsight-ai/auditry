"""Opt-in OpenTelemetry server spans for Quart. Requires the ``otel`` extra.

There is no official Quart instrumentation, so this wraps the ASGI app in
OpenTelemetry's generic ASGI middleware and names each span by its route.
"""

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import TracerProvider, get_current_span
from opentelemetry.util.http import ExcludeList
from quart import Quart, request

from ..models import ObservabilityConfig
from ..path_matcher import excluded_paths_to_url_patterns
from ..tracing import drop_or_raise

__all__ = ["instrument_quart"]


def instrument_quart(
    app: Quart, config: ObservabilityConfig, tracer_provider: TracerProvider | None = None
) -> None:
    """One server span per request, named by route rule, skipping
    ``config.excluded_paths`` (see ``excluded_paths_to_url_patterns`` for
    the dict-form limitation). No per-``receive``/``send`` child spans.

    Call it after every other ``app.asgi_app`` wrapper, so the span is
    active in all of them.
    """
    if isinstance(app.asgi_app, OpenTelemetryMiddleware):
        return
    patterns = excluded_paths_to_url_patterns(config.excluded_paths)
    app.asgi_app = OpenTelemetryMiddleware(  # type: ignore[assignment]
        app.asgi_app,
        excluded_urls=ExcludeList(patterns) if patterns else None,
        exclude_spans=["receive", "send"],
        tracer_provider=tracer_provider,
    )
    app.before_request(_name_span_by_route)


async def _name_span_by_route() -> None:
    """The generic ASGI middleware names spans by raw path; the route rule
    is known only once Quart has matched the request."""
    try:
        rule = request.url_rule
        span = get_current_span()
        if rule is not None and span.is_recording():
            span.update_name(f"{request.method} {rule.rule}")
            span.set_attribute("http.route", rule.rule)
    except Exception as exc:
        drop_or_raise("quart_span_name", exc)
