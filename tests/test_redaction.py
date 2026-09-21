"""Basic tests for redaction functionality."""

from auditry.redaction import redact_data, redact_headers


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
