"""Opt-in OpenTelemetry server spans for FastAPI. Requires the ``otel`` extra."""

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import TracerProvider

from ..models import ObservabilityConfig
from ..path_matcher import excluded_paths_to_url_patterns
from ..tracing import EXCLUDED_ASGI_SPANS

__all__ = ["instrument_fastapi"]


def instrument_fastapi(
    app: FastAPI, config: ObservabilityConfig, tracer_provider: TracerProvider | None = None
) -> None:
    """One server span per request, named by route template, skipping
    ``config.excluded_paths`` (see ``excluded_paths_to_url_patterns`` for
    the dict-form and Host header limitations). No per-``receive``/``send``
    child spans."""
    # The private marker FastAPIInstrumentor sets on an app it instrumented.
    # When the app or zero-code setup already instrumented it, a second pass
    # would mean two server spans per request.
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        return
    patterns = excluded_paths_to_url_patterns(config.excluded_paths)
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=",".join(patterns) or None,
        exclude_spans=list(EXCLUDED_ASGI_SPANS),
        tracer_provider=tracer_provider,
    )
