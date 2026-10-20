"""Span exporters that apply auditry's span scrubbing rules before export."""

from collections.abc import Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from ..span_scrubbing import (
    EXCEPTION_EVENT_NAME,
    EXCEPTION_TYPE_KEY,
    SpanScrubPolicy,
    get_span_scrub_policy,
    is_dropped_event,
    scrub_attributes,
    scrub_exception_event,
    scrub_span_name,
)
from ..tracing import drop_or_raise

__all__ = [
    "scrub_span",
    "ScrubbingSpanExporter",
    "ScrubbingOTLPSpanExporter",
    "ScrubbingConsoleSpanExporter",
]


def scrub_span(span: ReadableSpan, policy: SpanScrubPolicy) -> ReadableSpan:
    """A copy of ``span`` with every scrubbing rule applied.

    An ended span is immutable, so the copy is rebuilt through the public
    ``ReadableSpan`` constructor. The copy reports zero dropped attributes,
    events and links: those counts are not constructor arguments.
    """
    is_server = span.kind is SpanKind.SERVER
    attributes = scrub_attributes(span.attributes or {}, is_server=is_server, policy=policy)
    events = []
    exception_type = None
    for event in span.events:
        if is_dropped_event(event.name):
            continue
        event_attributes = dict(event.attributes or {})
        if event.name == EXCEPTION_EVENT_NAME:
            exception_type = exception_type or event_attributes.get(EXCEPTION_TYPE_KEY)
            event_attributes = scrub_exception_event(event_attributes)
        events.append(
            Event(
                event.name,
                scrub_attributes(event_attributes, is_server=False, policy=policy),
                event.timestamp,
            )
        )
    links = [
        Link(link.context, scrub_attributes(link.attributes or {}, is_server=False, policy=policy))
        for link in span.links
    ]
    status = span.status
    if status.description is not None:
        # The description repeats str(exc); only the exception type survives.
        description = exception_type if isinstance(exception_type, str) else None
        status = Status(
            status.status_code,
            description if status.status_code is StatusCode.ERROR else None,
        )
    return ReadableSpan(
        name=scrub_span_name(span.name, attributes, is_server=is_server),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    """Scrubs every span, then hands the batch to ``inner``.

    ```python
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(OTLPSpanExporter())))
    ```

    Fails closed: a span whose scrubbing raises is dropped, never exported
    unscrubbed, and the rest of the batch is exported. One warning per
    exception type per exporter, carrying no attribute values; strict mode
    raises instead.

    Without ``policy``, the process-wide policy is read at export time, so
    ``create_middleware`` may run after the exporter is set up.
    """

    def __init__(self, inner: SpanExporter, policy: SpanScrubPolicy | None = None) -> None:
        self._inner = inner
        self._policy = policy
        self._warned: set[tuple[str, str]] = set()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        policy = self._policy or get_span_scrub_policy()
        scrubbed = []
        for span in spans:
            try:
                scrubbed.append(scrub_span(span, policy))
            except Exception as exc:
                drop_or_raise("scrub_span", exc, self._warned)
        if not scrubbed:
            return SpanExportResult.SUCCESS
        return self._inner.export(scrubbed)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class ScrubbingOTLPSpanExporter(ScrubbingSpanExporter):
    """OTLP over HTTP, configured by the ``OTEL_EXPORTER_OTLP_*`` environment
    variables. Zero-code setup selects it with ``OTEL_TRACES_EXPORTER=auditry_otlp``."""

    def __init__(self) -> None:
        # Imported on construction: it loads protobuf and requests, which a
        # service wrapping its own exporter never needs.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        super().__init__(OTLPSpanExporter())


class ScrubbingConsoleSpanExporter(ScrubbingSpanExporter):
    """Scrubbed spans on stdout, for local development. Zero-code setup
    selects it with ``OTEL_TRACES_EXPORTER=auditry_console``."""

    def __init__(self) -> None:
        super().__init__(ConsoleSpanExporter())
