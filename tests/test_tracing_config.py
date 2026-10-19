"""Tests for TracingConfig defaults and validation."""

import pytest
from pydantic import ValidationError

from auditry import ObservabilityConfig, TracingConfig


def test_defaults():
    tracing = ObservabilityConfig(
        service_name="svc", log_request_body=False, log_response_body=False
    ).tracing
    assert tracing == TracingConfig()
    assert tracing.instrument_server is False
    assert tracing.query_param_allowlist == []
    assert tracing.db_statement == "sanitize"
    assert tracing.keep_client_address is False


@pytest.mark.parametrize("mode", ["sanitize", "drop", "keep"])
def test_db_statement_modes_accepted(mode):
    assert TracingConfig(db_statement=mode).db_statement == mode


def test_invalid_db_statement_rejected():
    with pytest.raises(ValidationError):
        TracingConfig(db_statement="redact")


def test_nested_dict_is_parsed():
    config = ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        tracing={"instrument_server": True, "query_param_allowlist": ["page"]},
    )
    assert config.tracing.instrument_server is True
    assert config.tracing.query_param_allowlist == ["page"]
