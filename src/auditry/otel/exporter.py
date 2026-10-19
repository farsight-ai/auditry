"""Span exporter that strips customer data before spans leave the process."""

import os
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Any, Optional, Union
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

from ..logging_config import get_logger, is_strict
from ..metrics import FORBIDDEN_DIMENSION_PATTERNS, _normalize
from ..otel_context import span_redaction_patterns
from ..redaction import DEFAULT_REDACTION_PATTERNS

__all__ = ["REDACTION_LEVEL_ENV", "RedactingSpanExporter", "RedactionLevel"]

_logger = get_logger("auditry.otel")

REDACTION_LEVEL_ENV = "AUDITRY_TRACE_REDACTION"


class RedactionLevel(str, Enum):
    """
    How much :class:`RedactingSpanExporter` removes. Each level removes
    everything the level below it does, plus:

    - ``NONE``: nothing; spans are exported as recorded.
    - ``LOW``: secrets. Header attributes, ``url.query``, the query string,
      fragment, and userinfo of URLs, and attributes matching the log
      redaction patterns (``token``, ``password``, ...).
    - ``MEDIUM``: customer data in attributes. Client address, user agent,
      and ``user.*`` identity, full URLs, raw paths (replaced by the route
      template), SQL statements, GenAI payloads (system instructions, tool
      call arguments and results, ``input.value``/``output.value``,
      ``traceloop.entity.*``, Langfuse input/output), and attributes
      matching the forbidden metric-dimension patterns (``email``,
      ``query``, ...) or ``content``.
    - ``HIGH`` (default): free text. Exception messages, stack traces, and
      status descriptions; only the exception type is kept.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def __ge__(self, other: "RedactionLevel") -> bool:  # type: ignore[override]
        return _LEVEL_ORDER.index(self) >= _LEVEL_ORDER.index(other)


_LEVEL_ORDER = (RedactionLevel.NONE, RedactionLevel.LOW, RedactionLevel.MEDIUM, RedactionLevel.HIGH)


def resolve_redaction_level(
    level: Optional[Union[RedactionLevel, str]] = None,
) -> RedactionLevel:
    """``level`` if given, else the ``AUDITRY_TRACE_REDACTION`` env var, else
    ``HIGH``. An unknown value raises, so a typo fails at startup instead of
    silently exporting at the wrong level."""
    value = level if level is not None else os.environ.get(REDACTION_LEVEL_ENV) or "high"
    try:
        return RedactionLevel(str(getattr(value, "value", value)).strip().lower())
    except ValueError:
        valid = ", ".join(lvl.value for lvl in RedactionLevel)
        raise ValueError(
            f"unknown span redaction level {value!r}; expected one of {valid}"
        ) from None


# Raw headers carry credentials (Authorization, Cookie, X-Api-Key).
_HEADER_PREFIXES = ("http.request.header.", "http.response.header.")
_QUERY_ATTRIBUTES = frozenset({"url.query"})

# Client identity: never operational data.
_CLIENT_PREFIXES = ("user.",)
_CLIENT_ATTRIBUTES = frozenset(
    {
        "client.address",
        "network.peer.address",
        "net.peer.ip",
        "http.client_ip",
        "user_agent.original",
        "http.user_agent",
    }
)

# A customer-supplied path is classified by its content (a file name, an
# object key, a search term), so raw paths are replaced by the route template,
# which developers wrote. Full URLs are dropped: the host is already in
# server.address / http.host and the path in http.route.
_ROUTE_ATTRIBUTE = "http.route"
_RAW_PATH_ATTRIBUTES = frozenset({"http.target", "url.path"})
_RAW_URL_ATTRIBUTES = frozenset({"http.url", "url.full"})

# GenAI instrumentation records prompt and response text under "content".
_TRACE_ONLY_PATTERNS = ("content",)

# GenAI payloads whose names match no pattern: semantic conventions,
# OpenInference (input.value), OpenLLMetry (traceloop.entity.input/output),
# and Langfuse.
_GENAI_PAYLOAD_PREFIXES = ("traceloop.entity.",)
_GENAI_PAYLOAD_ATTRIBUTES = frozenset(
    {
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "input.value",
        "output.value",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "langfuse.trace.output",
    }
)

# Statement text interpolates customer values. db.query.text is caught by the
# "query" pattern; db.statement (the pre-stable name) matches no pattern.
_STATEMENT_ATTRIBUTES = frozenset({"db.statement"})

# Semantic-convention keys that contain a sensitive substring but hold only
# operational data: a host name ("address"), counts and sizes ("token",
# "content", "message"), and opaque IDs. Caller-supplied patterns still
# apply to them. Source paths (code.filepath) are not listed: they can be
# absolute and name a user or directory.
_SAFE_ATTRIBUTES = frozenset(
    {
        "server.address",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "http.request_content_length",
        "http.response_content_length",
        "messaging.message.id",
        "messaging.message.body.size",
        "messaging.message.envelope.size",
        "messaging.batch.message_count",
    }
)

# An exception event keeps only its class; the message and stack trace can
# interpolate user content.
_EXCEPTION_EVENT = "exception"
_EXCEPTION_KEPT = frozenset({"exception.type", "exception.escaped"})


def _is_customer_data(key: str) -> bool:
    """Keys dropped from ``MEDIUM`` up whatever their name matches."""
    return (
        key in _CLIENT_ATTRIBUTES
        or key in _STATEMENT_ATTRIBUTES
        or key in _GENAI_PAYLOAD_ATTRIBUTES
        or key.startswith(_CLIENT_PREFIXES + _GENAI_PAYLOAD_PREFIXES)
    )


def _without_secrets(url: str) -> Optional[str]:
    """Scheme, host, port, and path only; ``None`` when ``url`` is unparsable."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


class RedactingSpanExporter(SpanExporter):
    """
    Wraps another exporter and removes customer data from every span first.

    Spans are immutable once ended, so redaction happens here, at export,
    rather than in a span processor. What is removed depends on ``level``
    (see :class:`RedactionLevel`); the default, ``HIGH``, lets only
    OPERATIONAL data leave. ``additional_patterns``, and the
    ``additional_redaction_patterns`` of every ``ObservabilityConfig`` passed
    to ``create_middleware`` (read at export time), apply at every level
    except ``NONE``, including to keys otherwise kept as operational.

    A failure while redacting or exporting drops the batch and logs one
    warning per error type; in strict mode it raises.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        additional_patterns: Optional[Iterable[str]] = None,
        level: Optional[Union[RedactionLevel, str]] = None,
    ) -> None:
        self._exporter = exporter
        self._level = resolve_redaction_level(level)
        patterns = [*DEFAULT_REDACTION_PATTERNS, *(additional_patterns or ())]
        if self._level >= RedactionLevel.MEDIUM:
            patterns += [*FORBIDDEN_DIMENSION_PATTERNS, *_TRACE_ONLY_PATTERNS]
        self._patterns = tuple(_normalize(p) for p in patterns)
        self._custom_patterns = tuple(_normalize(p) for p in additional_patterns or ())
        self._config_patterns: tuple[frozenset[str], tuple[str, ...]] = (frozenset(), ())
        self._warned: set[type[BaseException]] = set()
        if self._level is RedactionLevel.NONE:
            _logger.warning("span redaction is off; spans are exported as recorded")

    @property
    def level(self) -> RedactionLevel:
        return self._level

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._exporter.export(self.redact(spans))
        except Exception as error:
            self._report(error, "span export failed; dropping batch")
            return SpanExportResult.FAILURE

    def redact(self, spans: Sequence[ReadableSpan]) -> Sequence[ReadableSpan]:
        """``spans`` with customer data removed, per :attr:`level`."""
        if self._level is RedactionLevel.NONE:
            return spans
        custom = self._custom_patterns + self._registered_patterns()
        return [self._redact_span(span, custom) for span in spans]

    def _report(self, error: Exception, message: str) -> None:
        """Raise in strict mode; otherwise warn once per error type."""
        if is_strict():
            raise error
        if type(error) not in self._warned:
            self._warned.add(type(error))
            _logger.warning(message, error_type=type(error).__name__)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def _registered_patterns(self) -> tuple[str, ...]:
        """The config patterns, normalized once per change rather than per span."""
        registered = span_redaction_patterns()
        if registered is not self._config_patterns[0]:
            self._config_patterns = (registered, tuple(_normalize(p) for p in registered))
        return self._config_patterns[1]

    def _redact_span(self, span: ReadableSpan, custom: tuple[str, ...]) -> ReadableSpan:
        high = self._level >= RedactionLevel.HIGH
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=self._scrub(span.attributes, custom),
            events=[self._redact_event(event, custom) for event in span.events],
            links=[Link(link.context, self._scrub(link.attributes, custom)) for link in span.links],
            kind=span.kind,
            status=Status(span.status.status_code) if high else span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def _redact_event(self, event: Event, custom: tuple[str, ...]) -> Event:
        attributes = event.attributes or {}
        if event.name != _EXCEPTION_EVENT:
            attributes = self._scrub(attributes, custom)
        elif self._level >= RedactionLevel.HIGH:
            attributes = {k: v for k, v in attributes.items() if k in _EXCEPTION_KEPT}
        # Below HIGH the exception event is exported whole: pattern matching
        # would drop exception.message ("message") but keep the stack trace,
        # which ends with the same text.
        return Event(event.name, attributes, event.timestamp)

    def _scrub(
        self, attributes: Optional[Mapping[str, Any]], custom: tuple[str, ...]
    ) -> dict[str, Any]:
        attributes = attributes or {}
        route = attributes.get(_ROUTE_ATTRIBUTE)
        scrubbed: dict[str, Any] = {}
        for key, value in attributes.items():
            if key.startswith(_HEADER_PREFIXES) or key in _QUERY_ATTRIBUTES:
                continue
            if self._level >= RedactionLevel.MEDIUM and _is_customer_data(key):
                continue
            if key in _RAW_URL_ATTRIBUTES or key in _RAW_PATH_ATTRIBUTES:
                location = self._redact_location(key, value, route)
                if location is not None:
                    scrubbed[key] = location
            elif not self._is_sensitive(key, custom):
                scrubbed[key] = value
        return scrubbed

    def _redact_location(self, key: str, value: Any, route: Any) -> Any:
        """A URL or path attribute as exported, or ``None`` to drop it."""
        if self._level >= RedactionLevel.MEDIUM:
            return route if key in _RAW_PATH_ATTRIBUTES and route else None
        return _without_secrets(value) if isinstance(value, str) else None

    def _is_sensitive(self, key: str, custom: tuple[str, ...]) -> bool:
        normalized = _normalize(key)
        if any(pattern in normalized for pattern in custom):
            return True
        if key in _SAFE_ATTRIBUTES:
            return False
        return any(pattern in normalized for pattern in self._patterns)


class _RedactingSpanProcessor(SpanProcessor):
    """
    Hands ``processor`` spans redacted by ``redactor``. auditry's tracer
    provider wraps every processor added after it is built in one of these:
    FastAPI's auto-configure, for one, adds an OTLP exporter to the global
    provider at startup.

    A span that fails to redact is dropped, with the same warning and strict
    behavior as :class:`RedactingSpanExporter`.
    """

    def __init__(self, processor: SpanProcessor, redactor: RedactingSpanExporter) -> None:
        self._processor = processor
        self._redactor = redactor

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        self._processor.on_start(span, parent_context=parent_context)

    def _on_ending(self, span: Span) -> None:
        self._processor._on_ending(span)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            (redacted,) = self._redactor.redact([span])
        except Exception as error:
            self._redactor._report(error, "span redaction failed; dropping span")
            return
        self._processor.on_end(redacted)

    def shutdown(self) -> None:
        self._processor.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._processor.force_flush(timeout_millis)
