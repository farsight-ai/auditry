"""Tracer provider setup with redaction always in the export path."""

from collections.abc import Iterable
from typing import Optional, Union

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import Sampler

from ..logging_config import resolve_service_context
from .exporter import RedactingSpanExporter, RedactionLevel

__all__ = ["build_tracer_provider", "configure_tracing"]


def build_tracer_provider(
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
    :class:`~auditry.otel.exporter.RedactingSpanExporter`.

    ``service``/``version``/``environment`` resolve like
    :func:`auditry.configure_logging`, so spans and log lines agree. The
    exporter defaults to OTLP over HTTP, configured by the standard
    ``OTEL_EXPORTER_OTLP_*`` env vars. The sampler defaults to the SDK's,
    configured by ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``.
    ``redaction_level`` (``none``/``low``/``medium``/``high``) defaults to the
    ``AUDITRY_TRACE_REDACTION`` env var, then ``high``; see
    :class:`~auditry.otel.exporter.RedactionLevel`.
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
    provider = TracerProvider(resource=resource, sampler=sampler)
    provider.add_span_processor(
        BatchSpanProcessor(
            RedactingSpanExporter(
                exporter or OTLPSpanExporter(), additional_redaction_patterns, redaction_level
            )
        )
    )
    return provider


def configure_tracing(
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
        service,
        version,
        environment,
        exporter,
        sampler,
        additional_redaction_patterns,
        redaction_level,
    )
    otel_trace.set_tracer_provider(provider)
    return provider
