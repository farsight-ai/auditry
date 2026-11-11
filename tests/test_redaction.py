"""Basic tests for redaction functionality."""

import pytest

from auditry.redaction import (
    DEFAULT_REDACTION_ALLOWLIST,
    DEFAULT_REDACTION_PATTERNS,
    REDACTED_VALUE,
    SensitiveDataRedactor,
    redact_data,
    redact_headers,
)


def test_redact_password():
    """Test that password fields are redacted."""
    data = {"password": "secret123"}
    result = redact_data(data)
    assert result["password"] == "[REDACTED]"


def test_redact_api_key():
    """Test that api_key fields are redacted."""
    data = {"api_key": "abc123"}
    result = redact_data(data)
    assert result["api_key"] == "[REDACTED]"


def test_redact_nested():
    """Test that nested sensitive fields are redacted."""
    data = {"user": {"password": "secret", "name": "John"}}
    result = redact_data(data)
    assert result["user"]["password"] == "[REDACTED]"
    assert result["user"]["name"] == "John"


def test_redact_headers():
    """Test that authorization headers are redacted."""
    headers = {"authorization": "Bearer token123", "content-type": "application/json"}
    result = redact_headers(headers)
    assert result["authorization"] == "[REDACTED]"
    assert result["content-type"] == "application/json"


def test_redact_gateway_credential_header():
    """The gateway's credential header matches a default pattern."""
    result = redact_headers({"farsight-auth": "t", "X-Farsight-Auth": "t", "author": "a"})
    assert result == {"farsight-auth": "[REDACTED]", "X-Farsight-Auth": "[REDACTED]", "author": "a"}


def test_redact_leaves_strings_unscanned():
    """redact() matches field names only; redact_all() also scans text."""
    data = {"note": "token=abc"}
    assert redact_data(data) == {"note": "token=abc"}
    assert SensitiveDataRedactor().redact_all(data) == {"note": "token=[REDACTED]"}


class TestRedactText:
    """Credentials written into free text."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("GET /x?token=abc&page=2", "GET /x?token=[REDACTED]&page=2"),
            ('{"password": "hunter2", "user": "bob"}', '{"password": "[REDACTED]", "user": "bob"}'),
            ("{'api_key': 'k-1', 'n': 1}", "{'api_key': '[REDACTED]', 'n': 1}"),
            ('{"otp": 123456, "ok": true}', '{"otp": "[REDACTED]", "ok": true}'),
            ('{"body": "{\\"token\\": \\"abc\\"}"}', '{"body": "{\\"token\\": \\"[REDACTED]\\"}"}'),
            ("Authorization: Bearer abc.def-ghi", "Authorization: [REDACTED]"),
            ("Authorization: Basic dXNlcjpwYXNz", "Authorization: [REDACTED]"),
            ("farsight-auth: raw-credential", "farsight-auth: [REDACTED]"),
            ("--password=abc --user=x", "--password=[REDACTED] --user=x"),
            ("Access Token: abc123", "Access Token: [REDACTED]"),
            ("status: token=abc", "status: token=[REDACTED]"),
        ],
    )
    def test_value_after_a_sensitive_name(self, text, expected):
        assert SensitiveDataRedactor().redact_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("credentials={'user': 'u', 'key': 'k'} next=1", "credentials=[REDACTED] next=1"),
            ('"secret": {"value": "v"}, "ok": 1', '"secret": "[REDACTED]", "ok": 1'),
            ("password=SecretStr('raw') rest", "password=[REDACTED] rest"),
            ("    token = get_token(request)", "    token = [REDACTED]"),
        ],
    )
    def test_bracketed_value_is_redacted_whole(self, text, expected):
        assert SensitiveDataRedactor().redact_text(text) == expected

    def test_unterminated_value_runs_to_the_end(self):
        """A truncated body preview cuts the value short of its closing quote."""
        redacted = SensitiveDataRedactor().redact_text('{"user": "bob", "password": "hunt')
        assert redacted == '{"user": "bob", "password": "[REDACTED]"'

    def test_bearer_credential_without_a_name(self):
        redacted = SensitiveDataRedactor().redact_text("forwarded Bearer abc-123== upstream")
        assert redacted == "forwarded Bearer [REDACTED] upstream"

    def test_jwt_anywhere(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        assert SensitiveDataRedactor().redact_text(f"decoded {jwt} ok") == "decoded [REDACTED] ok"

    @pytest.mark.parametrize(
        "text",
        [
            "token rejected; password reset email sent",
            "if token == expected:",
            "if (token := next_token()):",
            "Failed: token expired",
            "plain message",
        ],
    )
    def test_text_without_a_named_value_is_unchanged(self, text):
        assert SensitiveDataRedactor().redact_text(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "password=[REDACTED]",
            '{"password": "[REDACTED]"}',
            "Authorization: [REDACTED]",
            "Bearer [REDACTED]",
        ],
    )
    def test_idempotent(self, text):
        assert SensitiveDataRedactor().redact_text(text) == text

    def test_additional_patterns_apply_to_text(self):
        redactor = SensitiveDataRedactor(["Tenant_Secret"])
        assert redactor.redact_text("tenant_secret=s-1") == "tenant_secret=[REDACTED]"


class TestRedactAll:
    """Field names at any depth, and every string."""

    def test_walks_tuples_and_scans_rendered_values(self):
        class Credentials:
            def __repr__(self):
                return "Credentials(api_key='k-1')"

        redacted = SensitiveDataRedactor().redact_all(
            {"items": ("token=a", {"secret": "s"}), "creds": Credentials(), "raw": b"password=p"}
        )
        assert redacted == {
            "items": ["token=[REDACTED]", {"secret": "[REDACTED]"}],
            "creds": "Credentials(api_key='[REDACTED]')",
            "raw": "b'password=[REDACTED]'",
        }

    def test_json_native_values_are_kept(self):
        data = {"count": 2, "ratio": 0.5, "ok": True, "none": None, "name": "n"}
        assert SensitiveDataRedactor().redact_all(data) == data


class TestAllowlist:
    """Names that match a pattern but are kept: the defaults, a service's own,
    and a service taking a default back off the list."""

    def test_every_default_name_matches_a_default_pattern(self):
        """An entry no pattern matches would never change anything."""
        redactor = SensitiveDataRedactor(allowlist=[])
        for name in DEFAULT_REDACTION_ALLOWLIST:
            assert any(pattern in name for pattern in DEFAULT_REDACTION_PATTERNS), name
            assert not redactor.should_redact(name), name

    @pytest.mark.parametrize(
        "name", ["prompt_tokens", "PromptTokens", "prompt-tokens", "PROMPT_TOKENS", "token_count"]
    )
    def test_a_default_name_is_kept_whatever_its_case_and_separators(self, name):
        assert redact_data({name: 12}) == {name: 12}

    @pytest.mark.parametrize("name", ["prompt_tokens_secret", "my_token_count", "prompt.tokens"])
    def test_only_a_whole_name_is_allowed(self, name):
        assert redact_data({name: "x"}) == {name: REDACTED_VALUE}

    def test_nested_usage_is_kept(self):
        usage = {"usage": {"input_tokens": 5, "cache_read_input_tokens": 2, "output_tokens": 7}}
        assert redact_data(usage) == usage

    def test_service_allowlist(self):
        data = {"approx_tokens": 5, "secret_id": "db-creds", "token": "t"}
        assert redact_data(data, allowlist=["approx_tokens", "Secret-ID"]) == {
            "approx_tokens": 5,
            "secret_id": "db-creds",
            "token": REDACTED_VALUE,
        }

    def test_a_pattern_naming_a_default_takes_it_off_the_list(self):
        redactor = SensitiveDataRedactor(["Prompt-Tokens"])
        assert redactor.should_redact("prompt_tokens")
        assert not redactor.should_redact("completion_tokens")

    def test_a_pattern_that_only_contains_a_default_leaves_it_on_the_list(self):
        assert not SensitiveDataRedactor(["prompt"]).should_redact("prompt_tokens")

    def test_the_service_allowlist_wins_over_its_own_patterns(self):
        redactor = SensitiveDataRedactor(["grant", "prompt_tokens"], ["grant_id", "prompt_tokens"])
        assert not redactor.should_redact("grant_id")
        assert not redactor.should_redact("prompt_tokens")
        assert redactor.should_redact("grant_secret")

    def test_an_allowed_value_is_still_scanned(self):
        redacted = SensitiveDataRedactor().redact_all({"token_type": "Bearer abc.def"})
        assert redacted == {"token_type": "Bearer [REDACTED]"}

    def test_text(self):
        redactor = SensitiveDataRedactor(allowlist=["approx_tokens"])
        assert (
            redactor.redact_text("approx_tokens=150000 TokenCount: 7 token=abc")
            == "approx_tokens=150000 TokenCount: 7 token=[REDACTED]"
        )
