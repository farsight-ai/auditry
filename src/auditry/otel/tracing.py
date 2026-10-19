"""Tracer provider setup with redaction always in the export path."""

from collections.abc import Iterable
from typing import Any, Optional, Union

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import Sampler

from ..logging_config import resolve_service_context
from ..otel_context import _enable_tracing
from .exporter import RedactingSpanExporter, RedactionLevel, _RedactingSpanProcessor

__all__ = ["build_tracer_provider", "configure_tracing"]


class _RedactingTracerProvider(TracerProvider):
    """A tracer provider whose span processors all receive redacted spans,
    including ones added after it is built."""

    def __init__(self, redactor: RedactingSpanExporter, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._redactor = redactor
        # The redactor's own batch is redacted on export, so it is not wrapped.
        super().add_span_processor(BatchSpanProcessor(redactor))

    def add_span_processor(self, span_processor: SpanProcessor) -> None:
        super().add_span_processor(_RedactingSpanProcessor(span_processor, self._redactor))


# Keyword-only settings, each optional with its own fallback; grouping them
# into an options object would only move the same list.
def build_tracer_provider(  # noqa: PLR0913
    *,
    service: Optional[str] = None,
    version: Optional[str] = None,
    environment: Optional[str] = None,
    exporter: Optional[SpanExporter] = None,
    sampler: Optional[Sampler] = None,
    additional_redaction_patterns: Optional[Iterable[str]] = None,
    redaction_level: Optional[Union[RedactionLevel, str]] = None,
) -> TracerProvider:
    """
    A tracer provider whose spans always pass through
    :class:`~auditry.otel.exporter.RedactingSpanExporter`, including spans
    handed to span processors added later (FastAPI's auto-configure adds an
    OTLP exporter to the global provider at startup).

    ``service``/``version``/``environment`` resolve like
    :func:`auditry.configure_logging`, so spans and log lines agree. The
    exporter defaults to OTLP over HTTP, configured by the standard
    ``OTEL_EXPORTER_OTLP_*`` env vars. The sampler defaults to the SDK's,
    configured by ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``.
    ``redaction_level`` (``none``/``low``/``medium``/``high``) defaults to the
    ``AUDITRY_TRACE_REDACTION`` env var, then ``high``; see
    :class:`~auditry.otel.exporter.RedactionLevel`.
    ``additional_redaction_patterns`` add to those of every
    ``ObservabilityConfig`` passed to ``create_middleware``.

    Building a provider opts the process in to auditry tracing (see
    :func:`auditry.otel_context.tracing_enabled`).
    """
    context = resolve_service_context(service, version, environment)
    resource = Resource.create(
        {
            key: value
            for key, value in {
                "service.name": context["service"],
                "service.version": context["version"],
                "deployment.environment.name": context["environment"],
            }.items()
            if value
        }
    )
    redactor = RedactingSpanExporter(
        exporter or OTLPSpanExporter(), additional_redaction_patterns, redaction_level
    )
    provider = _RedactingTracerProvider(redactor, resource=resource, sampler=sampler)
    _enable_tracing()
    return provider


def configure_tracing(  # noqa: PLR0913
    *,
    service: Optional[str] = None,
    version: Optional[str] = None,
    environment: Optional[str] = None,
    exporter: Optional[SpanExporter] = None,
    sampler: Optional[Sampler] = None,
    additional_redaction_patterns: Optional[Iterable[str]] = None,
    redaction_level: Optional[Union[RedactionLevel, str]] = None,
) -> TracerProvider:
    """
    Build a tracer provider (see :func:`build_tracer_provider`) and install it
    as the global one. Call once at startup, next to ``configure_logging``.

    OpenTelemetry allows the global provider to be set only once per process
    and ignores later attempts, so this raises if one is already installed
    (for example by ``opentelemetry-instrument``): spans would otherwise go to
    that provider, unredacted. Use :func:`build_tracer_provider` and pass it to
    ``create_middleware(..., tracer_provider=...)`` instead.
    """
    if not isinstance(otel_trace.get_tracer_provider(), otel_trace.ProxyTracerProvider):
        raise RuntimeError(
            "a global tracer provider is already installed; use build_tracer_provider() "
            "and pass it to create_middleware(tracer_provider=...)"
        )
    provider = build_tracer_provider(
        service=service,
        version=version,
        environment=environment,
        exporter=exporter,
        sampler=sampler,
        additional_redaction_patterns=additional_redaction_patterns,
        redaction_level=redaction_level,
    )
    otel_trace.set_tracer_provider(provider)
    return provider
