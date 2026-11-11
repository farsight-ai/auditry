import re
from typing import Any

DEFAULT_REDACTION_PATTERNS: list[str] = [
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "ssn",
    "social_security_number",
    "credit_card",
    "creditcard",
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "jwt",
    "bearer",
    "otp",
    "one_time_password",
    "access_key",
    "private_key",
    "signature",
    "credential",
    "x-auth",
    "farsight-auth",
    "x-amz-security-token",
    "proxy-authorization",
    "csrf",
    "xsrf",
]

# Names that contain a pattern but never hold a credential, so they are never
# replaced whole (their values are still scanned): LLM usage counts and limits
# as the OpenAI, Anthropic and Gemini SDKs name them, and OAuth's token_type
# (RFC 6749 5.1, "Bearer") and token_type_hint (RFC 7009 2.1, "refresh_token").
DEFAULT_REDACTION_ALLOWLIST: list[str] = [
    # OpenAI usage
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_tokens_details",
    "completion_tokens_details",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "audio_tokens",
    "text_tokens",
    "image_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    # Anthropic usage
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
    "output_tokens_details",
    "thinking_tokens",
    # Gemini usage
    "prompt_token_count",
    "candidates_token_count",
    "cached_content_token_count",
    "thoughts_token_count",
    "tool_use_prompt_token_count",
    "total_token_count",
    "candidates_tokens_details",
    "cache_tokens_details",
    "tool_use_prompt_tokens_details",
    "token_count",
    # Request limits
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    # OAuth
    "token_type",
    "token_type_hint",
]

REDACTED_VALUE = "[REDACTED]"


def _fold_name(name: str) -> str:
    """A name as the allow-list compares it: lowercased, without ``_`` or
    ``-``, so ``token_count``, ``TokenCount`` and ``token-count`` are one name."""
    return name.lower().replace("_", "").replace("-", "")


# Characters of a field or header name written in text. A name starts only
# where the previous character is not one of these, which keeps every scan
# below linear in the length of the text.
_NAME_CHARS = r"A-Za-z0-9_.\-"

# A name, then ":" or "=" (not "==", ":=" or "::", which are code), as JSON, a
# Python repr, a query string, logfmt or an HTTP header line writes it. "close"
# is the quote that ends a quoted name; a bare value is written back inside it.
_TEXT_NAME = re.compile(
    rf"(?<![{_NAME_CHARS}])(?P<name>[{_NAME_CHARS}]+)"
    r"(?P<close>\\?[\"'])?[ \t]*[:=](?![=:])[ \t]*"
)

# HTTP authentication schemes whose credentials follow after a space, so that
# "Authorization: Bearer abc" loses "Bearer abc", not just "Bearer".
_AUTH_SCHEMES = "basic|bearer|digest|negotiate|ntlm|token"

# The value after a name, read from where _TEXT_NAME ends. A quoted value
# with no closing quote (a truncated body preview) runs to the end of the text.
_TEXT_VALUE = re.compile(
    r'(?P<quote>")(?:[^"\\]|\\.)*(?:"|\\?\Z)'
    r"|(?P<single>')(?:[^'\\]|\\.)*(?:'|\\?\Z)"
    # an escaped quote: JSON written inside a JSON string
    r'|(?P<escaped>\\")(?:[^"\\]|\\[^"])*(?:\\"|\\?\Z)'
    rf"|(?:(?:{_AUTH_SCHEMES})[ \t]+)?[^\s,;&\"'<>(){{}}\[\]\\]+",
    re.IGNORECASE | re.DOTALL,
)

_OPENING_BRACKETS = "{[("
_CLOSING_BRACKETS = "}])"
_QUOTES = "\"'"

# A bearer credential with no name in front of it; the token is RFC 7235's token68.
_BEARER_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9_])(?P<scheme>bearer)[ \t]+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE
)

# A JSON Web Token anywhere in text: a header and a payload, both base64url
# JSON objects (so both start "eyJ"), and a signature that may be empty.
_JWT = re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]*\.eyJ[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]*")
# What every JWT contains, lowercased, for the fast path in redact_text.
_JWT_TRIGGER = "eyj"


def _bracket_end(text: str, start: int) -> int:
    """Index just past the bracket that closes the one at ``start``, skipping
    quoted strings; the end of the text when it never closes (truncated)."""
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in _QUOTES:
            quote = char
        elif char in _OPENING_BRACKETS:
            depth += 1
        elif char in _CLOSING_BRACKETS:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _json_fallback(value: Any) -> Any:
    """What structlog's JSONRenderer writes for a value JSON cannot encode:
    its ``__structlog__()``, else its ``repr()``."""
    render = getattr(value, "__structlog__", None)
    return render() if callable(render) else repr(value)


class SensitiveDataRedactor:
    """
    Redacts sensitive data from dictionaries, JSON-serializable objects and text.

    A field whose name contains a pattern (case-insensitive) has its value
    replaced with the redaction marker, in nested dictionaries and lists too,
    unless its whole name is on the allow-list.
    """

    def __init__(
        self, additional_patterns: list[str] | None = None, allowlist: list[str] | None = None
    ):
        """
        Initialize redactor with field patterns to redact.

        Args:
            additional_patterns: Additional field name patterns beyond defaults.
                One that is a ``DEFAULT_REDACTION_ALLOWLIST`` name, compared as
                the allow-list compares names, takes that name off the list.
            allowlist: Field names never replaced whole, beyond
                ``DEFAULT_REDACTION_ALLOWLIST``. They win over every pattern.
        """
        self.patterns: set[str] = {pattern.lower() for pattern in DEFAULT_REDACTION_PATTERNS}
        if additional_patterns:
            self.patterns.update(pattern.lower() for pattern in additional_patterns)
        retracted = {_fold_name(pattern) for pattern in self.patterns}
        self.allowlist: frozenset[str] = frozenset(
            {_fold_name(name) for name in DEFAULT_REDACTION_ALLOWLIST} - retracted
        ).union(_fold_name(name) for name in allowlist or ())

    def should_redact(self, field_name: str) -> bool:
        """
        Determine if a field should be redacted based on its name.

        Performs case-insensitive substring matching against known sensitive
        field patterns (``user_password`` matches ``password``). A name on the
        allow-list is never redacted; it must match an entry whole.

        Args:
            field_name: Name of the field to check

        Returns:
            True if field should be redacted, False otherwise
        """
        field_lower = field_name.lower()
        if not any(pattern in field_lower for pattern in self.patterns):
            return False
        return _fold_name(field_lower) not in self.allowlist

    def redact(self, data: Any) -> Any:
        """
        Recursively redact sensitive fields, by name, from a data structure.

        Handles dictionaries, lists, and primitive types. Creates a new
        structure with sensitive values replaced, leaving original unchanged.
        Strings are not scanned; see :meth:`redact_all`.

        Args:
            data: Data structure to redact (dict, list, or primitive)

        Returns:
            New data structure with sensitive values redacted
        """
        return self._redact(data, scan_text=False)

    def redact_all(self, data: Any) -> Any:
        """
        :meth:`redact`, plus :meth:`redact_text` over every string in ``data``.

        Tuples are walked like lists. Any other value JSON cannot encode is
        scanned as the text structlog's JSON renderer writes for it, so the
        rendered line carries what was scanned.
        """
        return self._redact(data, scan_text=True)

    def redact_text(self, text: str) -> str:
        """
        Redact credentials written into free text: a message, an exception
        message, a traceback, a body that is not JSON.

        - The value after a name that matches a pattern, followed by ``:`` or
          ``=``: ``token=abc``, ``"password": "x"``, ``{'api_key': 'k'}``,
          ``Authorization: Bearer abc``. A quoted value keeps its quotes; a
          bracketed one (``{...}``, ``[...]``, ``(...)``) is redacted whole.
        - The credential after ``Bearer``.
        - Any JSON Web Token.

        A credential written as prose ("the password is x") is not found.
        """
        # Every rule needs a pattern (a sensitive name, "bearer") or a JWT in
        # the text; most lines have neither and skip the scans.
        lowered = text.lower()
        if _JWT_TRIGGER not in lowered and not any(p in lowered for p in self.patterns):
            return text
        text = self._redact_named_values(text)
        text = _BEARER_CREDENTIAL.sub(lambda match: f"{match['scheme']} {REDACTED_VALUE}", text)
        return _JWT.sub(REDACTED_VALUE, text)

    def _redact(self, data: Any, scan_text: bool) -> Any:
        if isinstance(data, dict):
            return {
                key: REDACTED_VALUE
                if self.should_redact(str(key))
                else self._redact(value, scan_text)
                for key, value in data.items()
            }
        if isinstance(data, list) or (scan_text and isinstance(data, tuple)):
            return [self._redact(item, scan_text) for item in data]
        if not scan_text or data is None or isinstance(data, (bool, int, float)):
            return data
        if isinstance(data, str):
            return self.redact_text(data)
        return self._redact(_json_fallback(data), scan_text)

    def _redact_named_values(self, text: str) -> str:
        pieces = []
        cursor = 0
        for name in _TEXT_NAME.finditer(text):
            # A name inside a value already redacted is part of that value.
            if name.start() < cursor or not self.should_redact(name["name"]):
                continue
            value_end, marker = self._value_redaction(text, name.end(), name["close"])
            if value_end is None:
                continue
            pieces += [text[cursor : name.end()], marker]
            cursor = value_end
        if not pieces:
            return text
        pieces.append(text[cursor:])
        return "".join(pieces)

    @staticmethod
    def _value_redaction(text: str, start: int, close: str | None) -> tuple[int | None, str]:
        """Where the value at ``start`` ends, and what replaces it."""
        bare = (close or "") + REDACTED_VALUE + (close or "")
        if start < len(text) and text[start] in _OPENING_BRACKETS:
            return _bracket_end(text, start), bare
        value = _TEXT_VALUE.match(text, start)
        if value is None:
            return None, bare
        quote = value["quote"] or value["single"] or value["escaped"]
        if quote:
            return value.end(), quote + REDACTED_VALUE + quote
        end = value.end()
        # A constructor call, such as SecretStr('...'), is part of the value.
        if end < len(text) and text[end] in _OPENING_BRACKETS:
            end = _bracket_end(text, end)
        return end, bare


_default_redactor = SensitiveDataRedactor()


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """
    Redact sensitive headers for logging.

    Specifically handles common authentication and API key headers
    that should never appear in logs.

    Args:
        headers: Dictionary of HTTP headers

    Returns:
        New dictionary with sensitive headers redacted
    """
    return _default_redactor.redact(headers)


def redact_data(
    data: Any, additional_patterns: list[str] | None = None, allowlist: list[str] | None = None
) -> Any:
    """
    Convenience function to redact sensitive data.

    Args:
        data: Data structure to redact
        additional_patterns: Optional additional field patterns to redact
        allowlist: Optional field names never replaced whole, beyond the defaults

    Returns:
        Redacted copy of the data
    """
    if additional_patterns or allowlist:
        # Only create new instance if custom patterns are needed
        redactor = SensitiveDataRedactor(additional_patterns, allowlist)
        return redactor.redact(data)
    return _default_redactor.redact(data)
