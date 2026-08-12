"""Tests for the logging schema, error discipline, and config defaults."""

import json
import logging
import warnings

import pytest
import structlog
from asgi_correlation_id import correlation_id

from auditry import ObservabilityConfig
from auditry.logging_config import configure_logging, get_logger, set_trace_handler
from auditry.models import DEFAULT_EXCLUDED_PATHS


@pytest.fixture(autouse=True)
def reset():
    correlation_id.set(None)
    set_trace_handler(None)
    yield
    set_trace_handler(None)
    structlog.reset_defaults()


def configure_and_capture(capsys, **kwargs):
    configure_logging(**kwargs)
    logger = get_logger("test")
    return logger, capsys


def last_line(capsys):
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


class TestRootSchema:
    """Every line carries the root schema."""

    def test_schema_fields_present(self, capsys):
        logger, _ = configure_and_capture(
            capsys, service="test-svc", version="1.2.3", environment="test"
        )
        logger.info("hello", extra_field="x")
        rec = last_line(capsys)
        assert rec["service"] == "test-svc"
        assert rec["version"] == "1.2.3"
        assert rec["environment"] == "test"
        assert rec["level"] == "info"
        assert rec["message"] == "hello"       # message, not "event"
        assert "event" not in rec
        assert "timestamp" in rec
        assert rec["extra_field"] == "x"

    def test_env_var_fallback(self, capsys, monkeypatch):
        monkeypatch.setenv("SERVICE_NAME", "env-svc")
        monkeypatch.setenv("ENVIRONMENT", "prod")
        logger, _ = configure_and_capture(capsys)
        logger.info("hi")
        rec = last_line(capsys)
        assert rec["service"] == "env-svc"
        assert rec["environment"] == "prod"

    def test_correlation_id_on_every_line(self, capsys):
        # App-level lines carry the bound correlation ID automatically.
        logger, _ = configure_and_capture(capsys, service="s")
        correlation_id.set("cid-42")
        logger.info("with id")
        assert last_line(capsys)["correlation_id"] == "cid-42"

    def test_single_line_json(self, capsys):
        logger, _ = configure_and_capture(capsys, service="s")
        logger.info("one")
        logger.info("two")
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # every line parses independently


class TestErrorDiscipline:
    """error_type only on the standard stream; traces via the hook."""

    def test_exception_yields_error_type_not_traceback(self, capsys):
        logger, _ = configure_and_capture(capsys, service="s")
        try:
            raise ValueError("customer secret text")
        except ValueError:
            logger.error("failed", exc_info=True)
        rec = last_line(capsys)
        assert rec["error_type"] == "ValueError"
        assert "exception" not in rec
        # the message text must not leak into the standard stream
        assert "customer secret text" not in json.dumps(rec)

    def test_trace_handler_receives_full_trace(self, capsys):
        received = {}

        def handler(error_type, traceback_text, event_dict):
            received["error_type"] = error_type
            received["tb"] = traceback_text

        set_trace_handler(handler)
        logger, _ = configure_and_capture(capsys, service="s")
        try:
            raise KeyError("gated detail")
        except KeyError:
            logger.error("failed", exc_info=True)
        assert received["error_type"] == "KeyError"
        assert "gated detail" in received["tb"]
        # ... and still nothing on the standard stream
        assert "gated detail" not in json.dumps(last_line(capsys))

    def test_failing_trace_handler_does_not_break_logging(self, capsys):
        set_trace_handler(lambda *a: (_ for _ in ()).throw(RuntimeError("handler broke")))
        logger, _ = configure_and_capture(capsys, service="s")
        try:
            raise ValueError("x")
        except ValueError:
            logger.error("failed", exc_info=True)
        rec = last_line(capsys)
        assert rec["error_type"] == "ValueError"
        assert rec["trace_handler_error"] is True

    def test_dev_escape_hatch(self, capsys, monkeypatch):
        monkeypatch.setenv("AUDITRY_FULL_TRACEBACKS", "true")
        logger, _ = configure_and_capture(capsys, service="s")
        try:
            raise ValueError("dev detail")
        except ValueError:
            logger.error("failed", exc_info=True)
        rec = last_line(capsys)
        assert "dev detail" in rec["exception"]
        assert "\n" not in rec["exception"]  # still single-line


class TestConfigDefaults:
    def test_implicit_body_default_warns(self):
        with pytest.warns(DeprecationWarning, match="field-name redaction"):
            ObservabilityConfig(service_name="svc")

    def test_explicit_body_flags_do_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            ObservabilityConfig(
                service_name="svc", log_request_body=False, log_response_body=False
            )

    def test_default_health_paths_merged(self):
        cfg = ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False
        )
        for path in DEFAULT_EXCLUDED_PATHS:
            assert path in cfg.excluded_paths

    def test_default_health_paths_merge_with_user_list(self):
        cfg = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            excluded_paths=["/metrics"],
        )
        assert "/metrics" in cfg.excluded_paths
        assert "/healthz" in cfg.excluded_paths

    def test_default_health_paths_opt_out(self):
        cfg = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            include_default_excluded_paths=False,
        )
        assert cfg.excluded_paths is None

    def test_exception_messages_off_by_default(self):
        cfg = ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False
        )
        assert cfg.log_exception_messages is False


class TestQueryParamRedaction:
    """A token in a query string must not bypass redaction."""

    def test_query_params_redacted(self):
        from auditry.core.logger import RequestResponseLogger

        cfg = ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False
        )
        rrl = RequestResponseLogger(cfg)
        prepared = rrl.prepare_request_data(
            {
                "method": "GET",
                "path": "/x",
                "query_params": {"token": "sekrit", "page": "2"},
            },
            correlation_id="cid",
        )
        assert prepared["query_params"]["token"] == "[REDACTED]"
        assert prepared["query_params"]["page"] == "2"


class TestForeignStdlibRecords:
    """Records from plain stdlib loggers (uvicorn, boto3, any library) carry
    the same JSON root schema as structlog-originated lines — one stream,
    one schema."""

    def test_foreign_record_carries_schema(self, capsys):
        configure_logging(service="test-svc", version="1.2.3", environment="test")
        correlation_id.set("cid-foreign")
        logging.getLogger("some.library").info("plain %s", "message")
        rec = last_line(capsys)
        assert rec["message"] == "plain message"
        assert rec["service"] == "test-svc"
        assert rec["version"] == "1.2.3"
        assert rec["environment"] == "test"
        assert rec["correlation_id"] == "cid-foreign"
        assert rec["level"] == "info"
        assert "timestamp" in rec

    def test_foreign_exception_scrubbed_to_error_type(self, capsys):
        configure_logging(service="test-svc")
        try:
            raise ValueError("customer secret in foreign log")
        except ValueError:
            logging.getLogger("some.library").error("it failed", exc_info=True)
        rec = last_line(capsys)
        assert rec["error_type"] == "ValueError"
        raw = json.dumps(rec)
        assert "customer secret" not in raw
        assert "Traceback" not in raw
