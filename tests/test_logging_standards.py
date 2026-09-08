"""Tests for the logging schema, error discipline, and config defaults."""

import json
import logging
import warnings

import pytest
import structlog
from asgi_correlation_id import correlation_id

from auditry import ObservabilityConfig
from auditry.logging_config import (
    _set_config_service,
    _set_strict,
    configure_logging,
    get_logger,
    is_strict,
    set_trace_handler,
)
from auditry.models import DEFAULT_EXCLUDED_PATHS
from auditry.path_matcher import should_exclude_path


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AUDITRY_STRICT", raising=False)
    correlation_id.set(None)
    set_trace_handler(None)
    _set_config_service(None)
    _set_strict(False)
    yield
    set_trace_handler(None)
    _set_config_service(None)
    _set_strict(False)
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

    def test_trace_handler_receives_live_exc_info(self, capsys):
        import traceback

        received = {}

        def handler(error_type, exc_info, event_dict):
            received["error_type"] = error_type
            received["exc_info"] = exc_info
            received["event_dict"] = event_dict

        set_trace_handler(handler)
        logger, _ = configure_and_capture(capsys, service="s")
        try:
            raise KeyError("gated detail")
        except KeyError:
            logger.error("failed", exc_info=True)
        assert received["error_type"] == "KeyError"
        # The live (type, value, traceback) tuple — so error trackers can
        # capture the real exception object, and text is one render away.
        exc_type, exc_value, tb = received["exc_info"]
        assert exc_type is KeyError
        assert isinstance(exc_value, KeyError)
        assert "gated detail" in "".join(traceback.format_exception(exc_type, exc_value, tb))
        # The snapshot follows the root schema: log text under "message".
        assert received["event_dict"]["message"] == "failed"
        assert "event" not in received["event_dict"]
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
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1  # the stream line is still single-line ...
        rec = json.loads(lines[0])
        # ... but the decoded value is a real, tool-parseable traceback.
        assert rec["exception"].startswith("Traceback (most recent call last):")
        assert "\n" in rec["exception"]
        assert "dev detail" in rec["exception"]

    def test_dev_escape_hatch_is_read_at_configure_time(self, capsys, monkeypatch):
        # Resolved once by configure_logging(), like the service context —
        # flipping the env var afterwards has no effect until reconfigured.
        monkeypatch.setenv("AUDITRY_FULL_TRACEBACKS", "true")
        logger, _ = configure_and_capture(capsys, service="s")
        monkeypatch.delenv("AUDITRY_FULL_TRACEBACKS")
        try:
            raise ValueError("dev detail")
        except ValueError:
            logger.error("failed", exc_info=True)
        assert "dev detail" in last_line(capsys)["exception"]


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

    def test_default_health_paths_merge_into_wildcard_for_dict_form(self):
        """Dict-form configs get the same any-method probe exclusion as the
        list form: defaults merge into the "*" key, not GET — HEAD probes
        (common for ELB health checks) must be excluded too."""
        cfg = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            excluded_paths={"POST": ["/api/stream"]},
        )
        assert cfg.excluded_paths["POST"] == ["/api/stream"]
        for path in DEFAULT_EXCLUDED_PATHS:
            assert path in cfg.excluded_paths["*"]
        assert should_exclude_path("/healthz", "HEAD", cfg.excluded_paths)
        assert should_exclude_path("/healthz", "GET", cfg.excluded_paths)
        assert not should_exclude_path("/api/stream", "GET", cfg.excluded_paths)

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

    def test_foreign_exception_keeps_traceback(self, capsys):
        """Error discipline is scoped to auditry's own records. A stdlib
        logger.exception() in application or vendor code keeps its stack
        trace — stripping every except block in the process is not
        auditry's call to make."""
        configure_logging(service="test-svc")
        try:
            raise ValueError("app detail")
        except ValueError:
            logging.getLogger("myapp.payments").exception("charge failed")
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1  # still one JSON line on the stream
        rec = json.loads(lines[0])
        assert rec["message"] == "charge failed"
        assert rec["exception"].startswith("Traceback (most recent call last):")
        assert "app detail" in rec["exception"]
        assert "error_type" not in rec  # that field is auditry's own discipline


class TestServiceIdentity:
    """ObservabilityConfig.service_name is the one source of truth for
    ``service`` once middleware exists; configure_logging(service=) and
    SERVICE_NAME are the fallback for processes without middleware."""

    def test_config_wins_over_configure_logging(self, capsys):
        logger, _ = configure_and_capture(capsys, service="from-configure")
        _set_config_service("from-config")  # what create_middleware does
        logger.info("app line")
        assert last_line(capsys)["service"] == "from-config"

    def test_config_survives_configure_logging_called_afterwards(self, capsys):
        # create_middleware before configure_logging must not be wiped by the
        # latter's context reset.
        _set_config_service("from-config")
        logger, _ = configure_and_capture(capsys, service="from-configure")
        logger.info("app line")
        assert last_line(capsys)["service"] == "from-config"

    def test_foreign_records_use_the_same_identity(self, capsys):
        configure_logging(service="from-configure")
        _set_config_service("from-config")
        logging.getLogger("some.library").info("vendor line")
        assert last_line(capsys)["service"] == "from-config"

    def test_fallback_without_middleware(self, capsys):
        logger, _ = configure_and_capture(capsys, service="worker-svc")
        logger.info("worker line")
        assert last_line(capsys)["service"] == "worker-svc"

    def test_version_and_environment_still_come_from_configure_logging(self, capsys):
        logger, _ = configure_and_capture(
            capsys, service="x", version="9.9.9", environment="stage"
        )
        _set_config_service("from-config")
        logger.info("line")
        rec = last_line(capsys)
        assert rec["service"] == "from-config"
        assert rec["version"] == "9.9.9"
        assert rec["environment"] == "stage"


class TestStrictPolicy:
    """Instrumentation never fails the unit of work in production. In a KNOWN
    non-production environment the same failures raise. configure_logging()
    resolves the policy once: explicit > AUDITRY_STRICT > environment name."""

    @pytest.mark.parametrize(
        "env",
        ["local", "local-chris", "dev", "dev-feature-x", "development", "sandbox",
         "plat-sandbox", "test", "testing", "ci", "qa", "staging", "stage", "DEV"],
    )
    def test_known_non_production_names_are_strict(self, env):
        configure_logging(service="s", environment=env)
        assert is_strict() is True

    @pytest.mark.parametrize(
        "env", ["prod", "production", "customer-prod", "pitchbook", "prd", "live", None, ""]
    )
    def test_production_and_unknown_names_are_safe(self, env):
        # Unknown and unset are production: the rule is absolute in the
        # direction that never takes down a service call.
        configure_logging(service="s", environment=env)
        assert is_strict() is False

    def test_environment_env_var_drives_derivation(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "sandbox")
        configure_logging(service="s")
        assert is_strict() is True

    def test_auditry_strict_env_var_overrides_derivation(self, monkeypatch):
        monkeypatch.setenv("AUDITRY_STRICT", "1")
        configure_logging(service="s", environment="prod")
        assert is_strict() is True
        monkeypatch.setenv("AUDITRY_STRICT", "false")
        configure_logging(service="s", environment="dev")
        assert is_strict() is False

    def test_explicit_argument_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("AUDITRY_STRICT", "1")
        configure_logging(service="s", environment="dev", strict=False)
        assert is_strict() is False
        monkeypatch.setenv("AUDITRY_STRICT", "0")
        configure_logging(service="s", environment="prod", strict=True)
        assert is_strict() is True

    def test_not_strict_before_configure_logging(self):
        assert is_strict() is False

    def test_failing_trace_handler_raises_in_strict_mode(self, capsys):
        set_trace_handler(lambda *a: (_ for _ in ()).throw(RuntimeError("handler broke")))
        logger, _ = configure_and_capture(capsys, service="s", environment="test")
        with pytest.raises(RuntimeError, match="handler broke"):
            try:
                raise ValueError("x")
            except ValueError:
                logger.error("failed", exc_info=True)

    def test_failing_trace_handler_is_swallowed_in_production(self, capsys):
        set_trace_handler(lambda *a: (_ for _ in ()).throw(RuntimeError("handler broke")))
        logger, _ = configure_and_capture(capsys, service="s", environment="prod")
        try:
            raise ValueError("x")
        except ValueError:
            logger.error("failed", exc_info=True)
        assert last_line(capsys)["trace_handler_error"] is True
