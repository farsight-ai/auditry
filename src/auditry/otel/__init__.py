"""
Span exporters that scrub credentials, PII, customer content and exception
detail before export. Requires the ``otel`` extra.
"""

from .exporter import (
    ScrubbingConsoleSpanExporter,
    ScrubbingOTLPSpanExporter,
    ScrubbingSpanExporter,
    scrub_span,
)

__all__ = [
    "ScrubbingSpanExporter",
    "ScrubbingOTLPSpanExporter",
    "ScrubbingConsoleSpanExporter",
    "scrub_span",
]
