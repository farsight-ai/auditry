"""Opt-in OpenTelemetry server spans for FastAPI. Requires the ``otel`` extra."""

from fastapi import FastAPI
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import TracerProvider
from starlette.types import ASGIApp

from ..models import ObservabilityConfig
from ..otel.asgi import TracedUnlessExcluded
from ..tracing import EXCLUDED_ASGI_SPANS, drop_or_raise

__all__ = ["instrument_fastapi"]

# FastAPIInstrumentor's private marker on an app it instrumented.
INSTRUMENTED_MARKER = "_is_instrumented_by_opentelemetry"
NO_EXCLUDED_URLS = ""


class UnexpectedStackError(RuntimeError):
    """FastAPIInstrumentor built a middleware stack without
    OpenTelemetryMiddleware where this module expects it, so excluded paths
    cannot be kept out of tracing."""


def instrument_fastapi(
    app: FastAPI, config: ObservabilityConfig, tracer_provider: TracerProvider | None = None
) -> None:
    """One server span per request, named by route template, skipping
    ``config.excluded_paths`` exactly as request logging does, method-specific
    patterns included. No per-``receive``/``send`` child spans."""
    # When the app or zero-code setup already instrumented it, a second pass
    # would mean two server spans per request.
    if getattr(app, INSTRUMENTED_MARKER, False):
        return
    FastAPIInstrumentor.instrument_app(
        app,
        # Empty, not None: None falls back to OTEL_PYTHON_FASTAPI_EXCLUDED_URLS
        # (or OTEL_PYTHON_EXCLUDED_URLS), whose patterns match a URL built from
        # the client's Host header. TracedUnlessExcluded alone decides.
        excluded_urls=NO_EXCLUDED_URLS,
        exclude_spans=list(EXCLUDED_ASGI_SPANS),
        tracer_provider=tracer_provider,
    )
    instrumented_stack = app.build_middleware_stack

    def build_middleware_stack() -> ASGIApp:
        # FastAPIInstrumentor's stack is ServerErrorMiddleware around
        # OpenTelemetryMiddleware around the app's own stack. Putting the
        # exclusion check in OpenTelemetryMiddleware's place lets an excluded
        # request skip it into that same stack, built once.
        stack = instrumented_stack()
        traced = getattr(stack, "app", None)
        if isinstance(traced, OpenTelemetryMiddleware):
            stack.app = TracedUnlessExcluded(traced.app, traced, config.excluded_paths)  # type: ignore[attr-defined]
        else:
            drop_or_raise(
                "fastapi_excluded_paths",
                UnexpectedStackError(f"no OpenTelemetryMiddleware in {type(stack).__name__}"),
            )
        return stack

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]
