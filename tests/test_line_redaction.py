"""Line-level redaction: every line configure_logging() writes is redacted,
whoever logged it."""

import json
import logging

import pytest
from fastapi import FastAPI
from quart import Quart

from auditry import ObservabilityConfig, configure_logging, get_logger
from auditry.core.logger import RequestResponseLogger
from auditry.fastapi import create_middleware as create_fastapi_middleware
from auditry.logging_config import set_trace_handler
from auditry.metrics import MetricsLogger
from auditry.quart import create_middleware as create_quart_middleware

# A credential each test injects and then looks for in the captured output.
LEAKED = "leaked-credential-4f2a"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"

pytestmark = pytest.mark.usefixtures("clean_logging")


def captured_lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def only_line(capsys) -> dict:
    lines = captured_lines(capsys)
    assert len(lines) == 1
    return lines[0]


class TestAuditryRecords:
    """Lines from get_logger() and the middleware's RequestResponseLogger."""

    def test_message_fields_and_nested_values(self, capsys):
        configure_logging(service="svc")
        get_logger("t").info(
            f"calling upstream with token={LEAKED}",
            password=LEAKED,
            upstream={"headers": {"farsight-auth": LEAKED, "accept": "json"}},
            attempts=({"api_key": LEAKED}, f"Authorization: Bearer {LEAKED}"),
            note=f"decoded {JWT}",
        )
        line = only_line(capsys)
        assert line["message"] == "calling upstream with token=[REDACTED]"
        assert line["password"] == "[REDACTED]"
        assert line["upstream"] == {"headers": {"farsight-auth": "[REDACTED]", "accept": "json"}}
        assert line["attempts"] == [{"api_key": "[REDACTED]"}, "Authorization: [REDACTED]"]
        assert line["note"] == "decoded [REDACTED]"

    def test_value_json_cannot_encode_is_scanned_as_rendered(self, capsys):
        class Session:
            def __repr__(self):
                return f"Session(token='{LEAKED}')"

        configure_logging(service="svc")
        get_logger("t").info("session", session=Session())
        assert only_line(capsys)["session"] == "Session(token='[REDACTED]')"

    def test_inline_traceback_is_redacted(self, capsys, monkeypatch):
        monkeypatch.setenv("AUDITRY_FULL_TRACEBACKS", "true")
        configure_logging(service="svc")
        try:
            raise ValueError(f"rejected password={LEAKED}")
        except ValueError:
            get_logger("t").error("failed", exc_info=True)
        line = only_line(capsys)
        assert line["exception"].startswith("Traceback (most recent call last):")
        assert "rejected password=[REDACTED]" in line["exception"]
        assert LEAKED not in json.dumps(line)

    def test_opted_in_exception_message_is_redacted(self, capsys):
        configure_logging(service="svc")
        config = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            log_exception_messages=True,
        )
        rrl = RequestResponseLogger(config)
        request = rrl.prepare_request_data({"method": "GET", "path": "/x"})
        rrl.log_error(request, RuntimeError(f"upstream rejected api_key={LEAKED}"), 1.0)
        line = only_line(capsys)
        assert line["exception_message"] == "upstream rejected api_key=[REDACTED]"

    def test_bodies_the_middleware_cannot_parse_are_scanned(self, capsys):
        """A form body is not JSON, and a truncated JSON body no longer
        parses, so field-name redaction never sees either."""
        configure_logging(service="svc")
        config = ObservabilityConfig(
            service_name="svc", log_request_body=True, log_response_body=True, payload_size_limit=60
        )
        rrl = RequestResponseLogger(config)
        request = rrl.prepare_request_data(
            {"method": "POST", "path": "/login", "body": f"user=bob&password={LEAKED}".encode()}
        )
        response = rrl.prepare_response_data(
            {"status_code": 200, "body": json.dumps({"user": "bob", "token": LEAKED * 3}).encode()}
        )
        rrl.log_success(request, response, 1.0)
        line = only_line(capsys)
        assert line["request"]["body"] == "user=bob&password=[REDACTED]"
        assert line["response"]["body"]["_truncated"] is True
        assert line["response"]["body"]["_preview"] == '{"user": "bob", "token": "[REDACTED]"'

    def test_trace_handler_snapshot_is_redacted(self, capsys):
        received = {}
        set_trace_handler(lambda error_type, exc_info, event_dict: received.update(event_dict))
        configure_logging(service="svc")
        try:
            raise KeyError("k")
        except KeyError:
            get_logger("t").error(f"failed with token={LEAKED}", secret=LEAKED, exc_info=True)
        assert received["message"] == "failed with token=[REDACTED]"
        assert received["secret"] == "[REDACTED]"


class TestForeignRecords:
    """Lines from plain stdlib loggers in application or vendor code."""

    def test_formatted_message_is_redacted(self, capsys):
        configure_logging(service="svc")
        logging.getLogger("vendor.sdk").info("sending headers %s", {"farsight-auth": LEAKED})
        assert only_line(capsys)["message"] == "sending headers {'farsight-auth': '[REDACTED]'}"

    def test_traceback_is_redacted(self, capsys):
        configure_logging(service="svc")
        try:
            raise ConnectionError(f"401 for Authorization: Bearer {LEAKED}")
        except ConnectionError:
            logging.getLogger("vendor.sdk").exception("request failed")
        line = only_line(capsys)
        assert line["exception"].startswith("Traceback (most recent call last):")
        assert "401 for Authorization: [REDACTED]" in line["exception"]
        assert LEAKED not in json.dumps(line)


class TestPatterns:
    """Patterns beyond the defaults, and the fields no pattern replaces."""

    def test_configure_logging_patterns(self, capsys):
        configure_logging(service="svc", additional_redaction_patterns=["tenant_secret"])
        get_logger("t").info(f"rotated tenant_secret={LEAKED}", tenant_secret=LEAKED)
        line = only_line(capsys)
        assert line["message"] == "rotated tenant_secret=[REDACTED]"
        assert line["tenant_secret"] == "[REDACTED]"

    @pytest.mark.parametrize(
        "create_middleware", [create_fastapi_middleware, create_quart_middleware]
    )
    @pytest.mark.parametrize("configure_first", [True, False])
    def test_config_patterns_apply_to_every_line(self, capsys, create_middleware, configure_first):
        app = FastAPI() if create_middleware is create_fastapi_middleware else Quart(__name__)
        config = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            additional_redaction_patterns=["tenant_secret"],
        )
        if configure_first:
            configure_logging(service="svc")
            create_middleware(app, config)
        else:
            create_middleware(app, config)
            configure_logging(service="svc")
        logging.getLogger("app").info("rotated tenant_secret=%s", LEAKED)
        assert only_line(capsys)["message"] == "rotated tenant_secret=[REDACTED]"

    def test_schema_fields_are_never_replaced(self, capsys):
        """A pattern that matches a root-schema name still scans its value,
        but never blanks the field."""
        configure_logging(
            service="svc",
            environment="prod",
            additional_redaction_patterns=["message", "service", "environment", "level"],
        )
        get_logger("t").info(f"password={LEAKED}", chat_message="hi")
        line = only_line(capsys)
        assert line["message"] == "password=[REDACTED]"
        assert line["service"] == "svc"
        assert line["environment"] == "prod"
        assert line["level"] == "info"
        assert line["chat_message"] == "[REDACTED]"


class TestAllowlist:
    """Names that match a pattern but are kept on every line."""

    def test_default_allowlist(self, capsys):
        configure_logging(service="svc")
        get_logger("t").info(
            f"usage prompt_tokens=12 token={LEAKED}",
            usage={"input_tokens": 5, "output_tokens": 7},
            token_count=12,
            access_token=LEAKED,
        )
        line = only_line(capsys)
        assert line["message"] == "usage prompt_tokens=12 token=[REDACTED]"
        assert line["usage"] == {"input_tokens": 5, "output_tokens": 7}
        assert line["token_count"] == 12
        assert line["access_token"] == "[REDACTED]"

    def test_configure_logging_allowlist_and_retraction(self, capsys):
        configure_logging(
            service="svc",
            additional_redaction_patterns=["total_tokens"],
            redaction_allowlist=["approx_tokens"],
        )
        get_logger("t").info("limit", approx_tokens=150000, total_tokens=42, prompt_tokens=12)
        line = only_line(capsys)
        assert line["approx_tokens"] == 150000
        assert line["total_tokens"] == "[REDACTED]"
        assert line["prompt_tokens"] == 12

    @pytest.mark.parametrize(
        "create_middleware", [create_fastapi_middleware, create_quart_middleware]
    )
    def test_config_allowlist_applies_to_every_line(self, capsys, create_middleware):
        app = FastAPI() if create_middleware is create_fastapi_middleware else Quart(__name__)
        configure_logging(service="svc")
        create_middleware(
            app,
            ObservabilityConfig(
                service_name="svc",
                log_request_body=False,
                log_response_body=False,
                additional_redaction_patterns=["grant"],
                redaction_allowlist=["grant_id"],
            ),
        )
        logging.getLogger("app").info("issued grant_id=g-1 grant_secret=%s", LEAKED)
        assert only_line(capsys)["message"] == "issued grant_id=g-1 grant_secret=[REDACTED]"

    def test_config_allowlist_applies_to_bodies(self, capsys):
        configure_logging(service="svc")
        config = ObservabilityConfig(
            service_name="svc",
            log_request_body=True,
            log_response_body=True,
            redaction_allowlist=["approx_tokens"],
        )
        create_fastapi_middleware(FastAPI(), config)
        rrl = RequestResponseLogger(config)
        request = rrl.prepare_request_data({"method": "POST", "path": "/x"})
        body = {"usage": {"prompt_tokens": 3}, "approx_tokens": 9, "token": LEAKED}
        response = rrl.prepare_response_data(
            {"status_code": 200, "body": json.dumps(body).encode()}
        )
        rrl.log_success(request, response, 1.0)
        assert only_line(capsys)["response"]["body"] == {
            "usage": {"prompt_tokens": 3},
            "approx_tokens": 9,
            "token": "[REDACTED]",
        }


class TestMetricRecords:
    """EMF records ride the same pipeline: CloudWatch must still extract them."""

    def test_metric_named_like_a_pattern_keeps_its_value(self, capsys):
        configure_logging(service="svc", additional_redaction_patterns=["name", "unit"])
        MetricsLogger(namespace="Farsight/Test", service="svc").count(
            "TokensIssued", 3, dimensions={"TokenScope": "read", "TokenType": "refresh"}
        )
        line = only_line(capsys)
        assert line["TokensIssued"] == 3
        assert line["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
            {"Name": "TokensIssued", "Unit": "Count"}
        ]
        assert line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Farsight/Test"
        # A dimension is not a metric value: a matching one is redacted,
        # unless its name is on the allow-list.
        assert line["TokenScope"] == "[REDACTED]"
        assert line["TokenType"] == "refresh"
