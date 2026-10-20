"""
Span scrubbing rules: what an exported span may carry.

Traces are a general-access destination, so only system-generated
operational values (timings, status codes, route templates, random IDs,
error types) survive export. Credentials, PII, customer content, exception
messages and stack traces do not. The rules are pure functions over
attribute maps and import nothing from OpenTelemetry;
``auditry.otel.ScrubbingSpanExporter`` applies them to every exported span.
The README's "Tracing (OpenTelemetry)" section lists what each rule removes.
"""

import functools
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .redaction import REDACTED_VALUE, SensitiveDataRedactor

if TYPE_CHECKING:
    from .models import ObservabilityConfig

__all__ = [
    "SpanScrubPolicy",
    "set_span_scrub_policy",
    "get_span_scrub_policy",
    "scrub_attributes",
    "scrub_exception_event",
    "scrub_span_name",
    "is_dropped_event",
    "scrub_query",
    "scrub_url",
    "scrub_target",
    "sanitize_sql",
    "redact_emails",
]

DB_STATEMENT_MODES = ("sanitize", "drop", "keep")

EXCEPTION_EVENT_NAME = "exception"
EXCEPTION_DETAIL_KEYS = frozenset({"exception.message", "exception.stacktrace"})

URL_KEYS = frozenset({"url.full", "http.url", "url.query", "http.target"})
FULL_URL_KEYS = frozenset({"url.full", "http.url"})
# A fragment never leaves the client, so it carries no operational value, and
# it can carry tokens (OAuth implicit flow) or customer content.
URL_FRAGMENT_KEY = "url.fragment"

CLIENT_ADDRESS_KEYS = frozenset(
    {
        "client.address",
        "client.port",
        "net.peer.ip",
        "net.peer.port",
        "network.peer.address",
        "network.peer.port",
        "net.sock.peer.addr",
        "net.sock.peer.port",
        "http.client_ip",
    }
)

# Present only when the operator sets OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*.
HEADER_PREFIXES = ("http.request.header.", "http.response.header.")

IDENTITY_PREFIXES = ("enduser.", "user.")

DB_STATEMENT_KEYS = frozenset({"db.statement", "db.query.text"})
DB_PARAMETER_PREFIX = "db.query.parameter."

# GenAI attributes carrying prompts, responses or retrieved content, from
# https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
# (names change between semconv versions).
GENAI_CONTENT_KEYS = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.tool.definitions",
        "gen_ai.retrieval.query.text",
        "gen_ai.retrieval.documents",
        "gen_ai.evaluation.explanation",
    }
)
GENAI_CONTENT_PREFIXES = ("gen_ai.prompt.", "gen_ai.completion.")
GENAI_EVENT_PREFIX = "gen_ai."

# The Langfuse SDK attaches its span processor to the global tracer provider,
# so its spans reach this exporter too. Its langfuse.* attributes carry prompt
# input, model output and free-form metadata. Only these operational ones
# survive; every other langfuse.* key, including any a later SDK adds, is removed.
LANGFUSE_PREFIX = "langfuse."
LANGFUSE_OPERATIONAL_KEYS = frozenset(
    {
        "langfuse.observation.type",
        "langfuse.observation.level",
        "langfuse.observation.model.name",
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "langfuse.observation.prompt.name",
        "langfuse.observation.prompt.version",
        "langfuse.observation.completion_start_time",
        "langfuse.environment",
        "langfuse.release",
        "langfuse.version",
    }
)

# Anchored at the start of a run of local-part characters, so each run is
# tried once rather than from every position inside it.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_BYTES = re.compile(_EMAIL.pattern.encode())
# PostgreSQL dollar-quote delimiter, $$ or $tag$; a tag follows identifier
# rules minus "$". Not part of an identifier, which may itself contain "$".
_SQL_DOLLAR_DELIMITER = re.compile(r"(?<![\w$])\$(?P<tag>(?:[^\W\d]\w*)?)\$")
# One alternation so the leftmost opener wins: a quote inside a dollar-quoted
# string, or $$ inside a quoted one, is literal text.
_SQL_STRING_LITERAL = re.compile(
    r"'(?:[^']|'')*'|" + _SQL_DOLLAR_DELIMITER.pattern + r"(?s:.*?)\$(?P=tag)\$"
)
# Not part of an identifier (table2), a placeholder ($1) or a dotted name.
_SQL_NUMERIC_LITERAL = re.compile(r"(?<![\w$.])\d+(?:\.\d+)?(?![\w.])")


@dataclass(frozen=True)
class SpanScrubPolicy:
    """The configurable part of span scrubbing. Everything else is fixed."""

    query_param_allowlist: frozenset[str] = frozenset()
    redaction_patterns: tuple[str, ...] = ()
    db_statement: str = "sanitize"
    keep_client_address: bool = False

    def __post_init__(self) -> None:
        if self.db_statement not in DB_STATEMENT_MODES:
            raise ValueError(
                f"db_statement must be one of {DB_STATEMENT_MODES}, got {self.db_statement!r}"
            )

    @classmethod
    def from_config(cls, config: "ObservabilityConfig") -> "SpanScrubPolicy":
        """``config.tracing`` plus ``config.additional_redaction_patterns``."""
        tracing = config.tracing
        return cls(
            query_param_allowlist=frozenset(tracing.query_param_allowlist),
            redaction_patterns=tuple(config.additional_redaction_patterns or ()),
            db_statement=tracing.db_statement,
            keep_client_address=tracing.keep_client_address,
        )

    @property
    def redactor(self) -> SensitiveDataRedactor:
        """The default redaction patterns plus ``redaction_patterns``."""
        return _redactor(self.redaction_patterns)


@functools.lru_cache(maxsize=32)
def _redactor(patterns: tuple[str, ...]) -> SensitiveDataRedactor:
    return SensitiveDataRedactor(list(patterns))


_DEFAULT_POLICY = SpanScrubPolicy()
_policy = _DEFAULT_POLICY


def set_span_scrub_policy(policy: SpanScrubPolicy | None) -> None:
    """Set the process-wide policy used by exporters built without one
    (``create_middleware`` sets it from ObservabilityConfig). ``None``
    restores the default."""
    global _policy
    _policy = policy or _DEFAULT_POLICY


def get_span_scrub_policy() -> SpanScrubPolicy:
    return _policy


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _redact_emails_in_text(text: str) -> str:
    if "@" not in text and "%40" not in text:
        return text
    return _EMAIL.sub(REDACTED_VALUE, text)


def redact_emails(value: Any) -> Any:
    """Email addresses (``@`` or ``%40``) become ``[REDACTED]`` in a
    string, bytes, or any string inside a sequence or mapping value."""
    if isinstance(value, str):
        return _redact_emails_in_text(value)
    if isinstance(value, bytes):
        return _EMAIL_BYTES.sub(REDACTED_VALUE.encode(), value)
    if isinstance(value, Mapping):
        return {key: redact_emails(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return tuple(redact_emails(item) for item in value)
    return value


def scrub_query(query: str, policy: SpanScrubPolicy) -> str:
    """Scrub a raw query string (no leading ``?``): a value survives only
    when its key is allowlisted and matches no redaction pattern. Keys are
    kept. A query that does not parse is redacted whole."""
    if not query:
        return query
    try:
        # strict_parsing rejects a segment without "=" (``?q3-deck.pdf``),
        # which is a value rather than a key.
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return REDACTED_VALUE
    redactor = policy.redactor
    return urllib.parse.urlencode(
        [
            (
                key,
                value
                if key in policy.query_param_allowlist and not redactor.should_redact(key)
                else REDACTED_VALUE,
            )
            for key, value in pairs
        ],
        safe="[]",
    )


def scrub_url(url: str, policy: SpanScrubPolicy, route: str | None = None) -> str:
    """Scrub a full URL: userinfo and fragment stripped, query scrubbed. With
    a ``route``, the path becomes the route. A URL that does not parse is
    redacted whole."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return REDACTED_VALUE
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc.rpartition("@")[2],
            route if route else parts.path,
            scrub_query(parts.query, policy),
            "",
        )
    )


def scrub_target(target: str, policy: SpanScrubPolicy, route: str | None = None) -> str:
    """Scrub a request target (``path?query``), dropping any fragment. With a
    ``route``, the path becomes the route."""
    path, separator, query = target.partition("#")[0].partition("?")
    return (route if route else path) + separator + scrub_query(query, policy)


def sanitize_sql(statement: str) -> str | None:
    """``db_statement="sanitize"``: string literals (quoted and PostgreSQL
    dollar-quoted) and numeric literals become ``?``. Placeholders and
    identifiers are kept.

    Returns None when literal boundaries are ambiguous, so the statement is
    dropped rather than exported with a literal shifted into plain text: a
    backslash-escaped quote (an escape in some databases, an ordinary
    character in others), or a quote or dollar-quote delimiter still unpaired
    after sanitizing.
    """
    if "\\'" in statement:
        return None
    sanitized = _SQL_NUMERIC_LITERAL.sub("?", _SQL_STRING_LITERAL.sub("?", statement))
    if "'" in sanitized or _SQL_DOLLAR_DELIMITER.search(sanitized):
        return None
    return sanitized


def _is_sensitive_header(name: str, policy: SpanScrubPolicy) -> bool:
    redactor = policy.redactor
    # Older instrumentations normalize header names with "_" for "-".
    return redactor.should_redact(name) or redactor.should_redact(name.replace("_", "-"))


# ---------------------------------------------------------------------------
# Rules over one attribute map, in the order scrub_attributes applies them
# ---------------------------------------------------------------------------

_Attributes = dict[str, Any]


def _remove_identity(attributes: _Attributes) -> None:
    """End-user identity: every ``enduser.*`` and ``user.*`` key."""
    for key in [key for key in attributes if key.startswith(IDENTITY_PREFIXES)]:
        del attributes[key]


def _scrub_db_statements(attributes: _Attributes, mode: str) -> None:
    """Database statements per ``db_statement``; query parameters always go."""
    for key in list(attributes):
        if key.startswith(DB_PARAMETER_PREFIX):
            del attributes[key]
        elif key in DB_STATEMENT_KEYS and mode != "keep":
            value = attributes[key]
            sanitized = (
                sanitize_sql(value) if mode == "sanitize" and isinstance(value, str) else None
            )
            if sanitized is None:
                del attributes[key]
            else:
                attributes[key] = sanitized


def _remove_genai_content(attributes: _Attributes) -> None:
    """GenAI prompt, response and retrieved content."""
    for key in list(attributes):
        if key in GENAI_CONTENT_KEYS or key.startswith(GENAI_CONTENT_PREFIXES):
            del attributes[key]


def _remove_langfuse_content(attributes: _Attributes) -> None:
    """Langfuse SDK attributes other than the operational ones."""
    for key in list(attributes):
        if key.startswith(LANGFUSE_PREFIX) and key not in LANGFUSE_OPERATIONAL_KEYS:
            del attributes[key]


def _scrub_urls(attributes: _Attributes, policy: SpanScrubPolicy, route: str | None) -> None:
    """Query strings in every URL key, fragments removed; with a ``route``,
    paths become it."""
    attributes.pop(URL_FRAGMENT_KEY, None)
    for key in URL_KEYS & attributes.keys():
        value = attributes[key]
        if not isinstance(value, str):
            del attributes[key]
        elif key in FULL_URL_KEYS:
            attributes[key] = scrub_url(value, policy, route)
        elif key == "http.target":
            attributes[key] = scrub_target(value, policy, route)
        else:
            attributes[key] = scrub_query(value, policy)
    if route and "url.path" in attributes:
        attributes["url.path"] = route


def _remove_client_address(attributes: _Attributes) -> None:
    """The client's IP address and port (server spans)."""
    for key in CLIENT_ADDRESS_KEYS & attributes.keys():
        del attributes[key]


def _redact_sensitive_headers(attributes: _Attributes, policy: SpanScrubPolicy) -> None:
    """Captured header values whose name matches a redaction pattern."""
    for key in attributes:
        for prefix in HEADER_PREFIXES:
            if key.startswith(prefix) and _is_sensitive_header(key[len(prefix) :], policy):
                attributes[key] = (REDACTED_VALUE,)


def scrub_attributes(
    attributes: Mapping[str, Any], *, is_server: bool, policy: SpanScrubPolicy
) -> dict[str, Any]:
    """Every attribute rule, in order, over one span, event or link
    attribute map. Route paths and client-address removal apply only when
    ``is_server`` is true; email redaction runs last. Returns a new map."""
    scrubbed = dict(attributes)
    _remove_identity(scrubbed)
    _scrub_db_statements(scrubbed, policy.db_statement)
    _remove_genai_content(scrubbed)
    _remove_langfuse_content(scrubbed)
    route = scrubbed.get("http.route") if is_server else None
    _scrub_urls(scrubbed, policy, route if isinstance(route, str) else None)
    if is_server and not policy.keep_client_address:
        _remove_client_address(scrubbed)
    _redact_sensitive_headers(scrubbed, policy)
    return {key: redact_emails(value) for key, value in scrubbed.items()}


def scrub_exception_event(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """An ``exception`` event keeps ``exception.type`` and
    ``exception.escaped``; the message and stack trace are removed."""
    return {key: value for key, value in attributes.items() if key not in EXCEPTION_DETAIL_KEYS}


def scrub_span_name(name: str, attributes: Mapping[str, Any], *, is_server: bool) -> str:
    """Server spans are named ``METHOD route`` (or ``METHOD``); any name is
    cut at ``?`` and has email addresses redacted. ``attributes`` are the
    span's attributes, scrubbed or not: the method and route survive."""
    if is_server:
        method = attributes.get("http.request.method") or attributes.get("http.method")
        if isinstance(method, str) and method:
            method = "HTTP" if method == "_OTHER" else method
            route = attributes.get("http.route")
            name = f"{method} {route}" if isinstance(route, str) and route else method
    return _redact_emails_in_text(name.split("?", 1)[0])


def is_dropped_event(name: str) -> bool:
    """GenAI events carry prompt and response content."""
    return name.startswith(GENAI_EVENT_PREFIX)
