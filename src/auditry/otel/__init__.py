"""OpenTelemetry tracing for auditry. Requires the otel extra."""

from .exporter import REDACTION_LEVEL_ENV, RedactingSpanExporter, RedactionLevel
from .middleware import TracingMiddleware
from .tracing import build_tracer_provider, configure_tracing

__all__ = [
    "REDACTION_LEVEL_ENV",
    "RedactingSpanExporter",
    "RedactionLevel",
    "TracingMiddleware",
    "build_tracer_provider",
    "configure_tracing",
]
