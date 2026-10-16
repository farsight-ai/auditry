"""Tests for OpenTelemetry tracing: log correlation, server spans, span
redaction, and trace-context propagation."""

import json
import logging
import sys

import pytest
from asgi_correlation_id import correlation_id
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from quart import Quart

from auditry import (
    ObservabilityConfig,
    configure_logging,
    get_logger,
    outbound_headers,
    sqs_message_attributes,
    trace_context,
    with_correlation,
)
from auditry import otel_context as auditry_otel_context
from auditry.fastapi import create_middleware as create_fastapi_middleware
from auditry.logging_config import _set_strict
from auditry.otel import (
    REDACTION_LEVEL_ENV,
    RedactingSpanExporter,
    RedactionLevel,
    build_tracer_provider,
    configure_tracing,
)
from auditry.otel import tracing as auditry_tracing
from auditry.otel_context import current_trace_ids
from auditry.quart import create_middleware as create_quart_middleware

SECRET = "s3cr3t-customer-value"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"


class Tracing:
    """A redacting tracer provider exporting into memory."""

    def __init__(self) -> None:
        self.memory = InMemorySpanExporter()
        self.provider = build_tracer_provider(service="svc", exporter=self.memory)
        self.tracer = self.provider.get_tracer("test")

    def spans(self):
        self.provider.force_flush()
        return self.memory.get_finished_spans()

    def server_spans(self):
        return [s for s in self.spans() if s.kind is otel_trace.SpanKind.SERVER]


@pytest.fixture
def tracing():
    t = Tracing()
    yield t
    t.provider.shutdown()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(REDACTION_LEVEL_ENV, raising=False)
    configure_logging(service="svc")
    _set_strict(False)
    token = correlation_id.set(None)
    yield
    correlation_id.reset(token)
    _set_strict(False)


def _config(**overrides):
    return ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        tracing=True,
        **overrides,
    )


def _capture_logs():
    # Called in the test body: capsys only swaps sys.stdout for the call
    # phase, and the log handler binds to whatever sys.stdout is when
    # configure_logging runs.
    configure_logging(service="svc")


def _log_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def _request_line(capsys):
    lines = [
        line
        for line in _log_lines(capsys)
        if line["message"].startswith(("Request completed", "Request failed"))
    ]
    assert len(lines) == 1
    return lines[0]


def _hex_trace_id(span):
    return otel_trace.format_trace_id(span.context.trace_id)


# ---------------------------------------------------------------------------
# Log correlation
# ---------------------------------------------------------------------------


class TestLogCorrelation:
    def test_log_line_inside_span_carries_trace_and_span_id(self, tracing, capsys):
        _capture_logs()
        with tracing.tracer.start_as_current_span("work") as span:
            get_logger("test").info("inside")
        line = _log_lines(capsys)[-1]
        context = span.get_span_context()
        assert line["trace_id"] == otel_trace.format_trace_id(context.trace_id)
        assert line["span_id"] == otel_trace.format_span_id(context.span_id)

    def test_log_line_outside_span_has_no_trace_id(self, capsys):
        _capture_logs()
        get_logger("test").info("outside")
        line = _log_lines(capsys)[-1]
        assert "trace_id" not in line
        assert "span_id" not in line


# ---------------------------------------------------------------------------
# Server spans (FastAPI and Quart)
# ---------------------------------------------------------------------------


def _fastapi_app(tracing, **config_overrides):
    app = FastAPI()
    create_fastapi_middleware(app, _config(**config_overrides), tracer_provider=tracing.provider)

    @app.get("/items/{item_id}")
    async def get_item(item_id: str):
        return {"item_id": item_id}

    @app.get("/fail/{item_id}")
    async def fail(item_id: str):
        raise ValueError(f"customer secret {SECRET}")

    @app.get("/files/{name}")
    async def get_file(name: str):
        return {"found": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def _quart_app(tracing):
    app = Quart(__name__)
    create_quart_middleware(app, _config(), tracer_provider=tracing.provider)

    @app.route("/items/<item_id>")
    async def get_item(item_id):
        return {"item_id": item_id}

    @app.route("/health")
    async def health():
        return {"ok": True}

    return app.test_client()


class TestFastAPISpans:
    def test_span_named_by_route_template_with_correlation_id(self, tracing):
        response = _fastapi_app(tracing).get("/items/42")
        (span,) = tracing.server_spans()
        assert span.name == "GET /items/{item_id}"
        assert span.attributes["http.route"] == "/items/{item_id}"
        assert span.attributes["correlation_id"] == response.headers["X-Request-ID"]

    def test_request_log_line_carries_the_span_trace_id(self, tracing, capsys):
        _capture_logs()
        _fastapi_app(tracing).get("/items/42")
        (span,) = tracing.server_spans()
        assert _request_line(capsys)["trace_id"] == _hex_trace_id(span)

    def test_excluded_path_is_not_traced(self, tracing):
        _fastapi_app(tracing).get("/health")
        assert tracing.spans() == ()

    def test_inbound_traceparent_is_continued_not_regenerated(self, tracing):
        _fastapi_app(tracing).get("/items/42", headers={"traceparent": TRACEPARENT})
        (span,) = tracing.server_spans()
        assert _hex_trace_id(span) == TRACE_ID
        assert otel_trace.format_span_id(span.parent.span_id) == PARENT_SPAN_ID

    def test_mounted_route_name_matches_otel_http_route(self, tracing):
        app = FastAPI()
        create_fastapi_middleware(app, _config(), tracer_provider=tracing.provider)
        api = FastAPI()

        @api.get("/items/{item_id:int}")
        async def get_item(item_id: int):
            return {"item_id": item_id}

        app.mount("/v1", api)
        TestClient(app).get("/v1/items/42")
        (span,) = tracing.server_spans()
        assert span.name == "GET /v1/items/{item_id}"
        assert span.attributes["http.route"] == "/v1/items/{item_id}"

    def test_tracing_off_creates_no_spans(self, tracing):
        app = FastAPI()
        create_fastapi_middleware(
            app,
            ObservabilityConfig(
                service_name="svc", log_request_body=False, log_response_body=False
            ),
            tracer_provider=tracing.provider,
        )

        @app.get("/items/{item_id}")
        async def get_item(item_id: str):
            return {"item_id": item_id}

        TestClient(app).get("/items/42")
        assert tracing.spans() == ()


class TestQuartSpans:
    @pytest.mark.asyncio
    async def test_span_named_by_route_template_with_correlation_id(self, tracing):
        response = await _quart_app(tracing).get("/items/42")
        (span,) = tracing.server_spans()
        assert span.name == "GET /items/<item_id>"
        assert span.attributes["http.route"] == "/items/<item_id>"
        assert span.attributes["correlation_id"] == response.headers["X-Request-ID"]

    @pytest.mark.asyncio
    async def test_request_log_line_carries_the_span_trace_id(self, tracing, capsys):
        _capture_logs()
        await _quart_app(tracing).get("/items/42")
        (span,) = tracing.server_spans()
        assert _request_line(capsys)["trace_id"] == _hex_trace_id(span)

    @pytest.mark.asyncio
    async def test_excluded_path_is_not_traced(self, tracing):
        await _quart_app(tracing).get("/health")
        assert tracing.spans() == ()


class TestTracingConfig:
    def test_tracing_without_the_extra_fails_at_startup(self, monkeypatch):
        # Simulate opentelemetry being absent: the parent package cannot be
        # imported, which is what makes find_spec raise.
        monkeypatch.delitem(sys.modules, "opentelemetry.instrumentation.asgi")
        monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation", None)
        with pytest.raises(ValueError, match="otel extra"):
            _config()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestSpansDoNotLeak:
    def test_request_with_secrets_exports_none_of_them(self, tracing):
        _fastapi_app(tracing).get(
            f"/fail/1?access_token={SECRET}",
            headers={"Authorization": f"Bearer {SECRET}", "User-Agent": f"agent-{SECRET}"},
        )
        spans = tracing.spans()
        assert spans
        for span in spans:
            assert SECRET not in span.to_json()

    def test_exception_event_keeps_only_the_type(self, tracing):
        _fastapi_app(tracing).get("/fail/1")
        (span,) = tracing.server_spans()
        (event,) = [e for e in span.events if e.name == "exception"]
        assert event.attributes["exception.type"] == "ValueError"
        assert "exception.message" not in event.attributes
        assert "exception.stacktrace" not in event.attributes
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None

    def test_attributes_are_scrubbed_and_operational_ones_kept(self, tracing):
        with tracing.tracer.start_as_current_span("work") as span:
            span.set_attributes(
                {
                    "url.full": f"https://api.example.com/files/{SECRET}.pdf?q={SECRET}",
                    "url.path": f"/files/{SECRET}.pdf",
                    "http.target": f"/files/{SECRET}.pdf",
                    "http.route": "/files/{name}",
                    "url.query": f"q={SECRET}",
                    "gen_ai.message.content": SECRET,
                    "http.request.header.authorization": SECRET,
                    "client.address": "203.0.113.7",
                    "app.user_email": SECRET,
                    "db.query.text": f"SELECT '{SECRET}'",
                    "auth.token": SECRET,
                    "http.request.method": "GET",
                    "http.response.status_code": 200,
                    "server.address": "api.example.com",
                    "correlation_id": "abc123",
                }
            )
        (exported,) = tracing.spans()
        assert dict(exported.attributes) == {
            "url.path": "/files/{name}",
            "http.target": "/files/{name}",
            "http.route": "/files/{name}",
            "http.request.method": "GET",
            "http.response.status_code": 200,
            "server.address": "api.example.com",
            "correlation_id": "abc123",
        }

    def test_raw_path_without_a_route_is_dropped(self, tracing):
        with tracing.tracer.start_as_current_span("work") as span:
            span.set_attributes({"http.target": f"/files/{SECRET}", "http.method": "GET"})
        (exported,) = tracing.spans()
        assert dict(exported.attributes) == {"http.method": "GET"}

    def test_customer_value_in_the_path_is_not_exported(self, tracing):
        _fastapi_app(tracing).get(f"/files/{SECRET}.pdf")
        (span,) = tracing.server_spans()
        assert span.attributes["http.target"] == "/files/{name}"
        assert SECRET not in span.to_json()

    def test_operational_keys_survive_pattern_collisions(self, tracing):
        operational = {
            "gen_ai.usage.input_tokens": 12,
            "http.response_content_length": 512,
            "messaging.message.id": "m-1",
            "code.filepath": "app/main.py",
        }
        with tracing.tracer.start_as_current_span("work") as span:
            span.set_attributes({**operational, "db.statement": f"SELECT '{SECRET}'"})
        (exported,) = tracing.spans()
        assert dict(exported.attributes) == operational

    def test_additional_patterns_are_applied(self):
        memory = InMemorySpanExporter()
        provider = build_tracer_provider(
            exporter=memory, additional_redaction_patterns=["account_number"]
        )
        with provider.get_tracer("test").start_as_current_span("work") as span:
            span.set_attribute("app.account_number", SECRET)
        provider.force_flush()
        (exported,) = memory.get_finished_spans()
        assert "app.account_number" not in exported.attributes


_LEVEL_ATTRIBUTES = {
    "url.full": f"https://user:{SECRET}@api.example.com/files/{SECRET}.pdf?q={SECRET}#frag",
    "http.target": f"/files/{SECRET}.pdf?access_token={SECRET}",
    "url.path": f"/files/{SECRET}.pdf",
    "http.route": "/files/{name}",
    "url.query": f"q={SECRET}",
    "http.request.header.authorization": SECRET,
    "auth.token": SECRET,
    "client.address": "203.0.113.7",
    "app.user_email": SECRET,
    "http.request.method": "GET",
}


def _export_at(level, attributes=_LEVEL_ATTRIBUTES, error=None):
    """One span, recorded with ``attributes`` and optionally ``error``,
    exported through a provider at ``level``."""
    memory = InMemorySpanExporter()
    provider = build_tracer_provider(exporter=memory, redaction_level=level)
    with provider.get_tracer("test").start_as_current_span("work") as span:
        span.set_attributes(attributes)
        if error:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
    provider.force_flush()
    (exported,) = memory.get_finished_spans()
    return exported


class TestRedactionLevels:
    def test_default_is_high(self):
        assert RedactingSpanExporter(InMemorySpanExporter()).level is RedactionLevel.HIGH

    def test_sql_statement_is_kept_at_low_and_dropped_from_medium(self):
        statement = {"db.statement": "SELECT 1"}
        assert dict(_export_at("low", statement).attributes) == statement
        assert dict(_export_at("medium", statement).attributes) == {}

    def test_env_var_sets_the_level(self, monkeypatch):
        monkeypatch.setenv(REDACTION_LEVEL_ENV, "Medium")
        assert RedactingSpanExporter(InMemorySpanExporter()).level is RedactionLevel.MEDIUM

    def test_argument_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv(REDACTION_LEVEL_ENV, "none")
        exporter = RedactingSpanExporter(InMemorySpanExporter(), level="low")
        assert exporter.level is RedactionLevel.LOW

    def test_unknown_level_fails_at_startup(self):
        with pytest.raises(ValueError, match="unknown span redaction level"):
            build_tracer_provider(exporter=InMemorySpanExporter(), redaction_level="hgih")

    def test_none_exports_spans_as_recorded(self, caplog):
        with caplog.at_level(logging.WARNING, logger="auditry.otel"):
            exported = _export_at(RedactionLevel.NONE, error=ValueError(SECRET))
        assert dict(exported.attributes) == _LEVEL_ATTRIBUTES
        assert exported.status.description == SECRET
        assert "span redaction is off" in caplog.text

    def test_low_removes_secrets_only(self):
        exported = _export_at("low")
        assert dict(exported.attributes) == {
            "url.full": f"https://api.example.com/files/{SECRET}.pdf",
            "http.target": f"/files/{SECRET}.pdf",
            "url.path": f"/files/{SECRET}.pdf",
            "http.route": "/files/{name}",
            "client.address": "203.0.113.7",
            "app.user_email": SECRET,
            "http.request.method": "GET",
        }

    def test_medium_removes_customer_data_and_keeps_errors(self):
        exported = _export_at("medium", error=ValueError(SECRET))
        assert dict(exported.attributes) == {
            "http.target": "/files/{name}",
            "url.path": "/files/{name}",
            "http.route": "/files/{name}",
            "http.request.method": "GET",
        }
        (event,) = exported.events
        assert event.attributes["exception.message"] == SECRET
        assert exported.status.description == SECRET

    def test_high_also_removes_error_text(self):
        exported = _export_at("high", error=ValueError(SECRET))
        assert SECRET not in exported.to_json()
        (event,) = exported.events
        assert event.attributes["exception.type"] == "ValueError"

    def test_additional_patterns_apply_from_low(self):
        memory = InMemorySpanExporter()
        provider = build_tracer_provider(
            exporter=memory, additional_redaction_patterns=["account"], redaction_level="low"
        )
        with provider.get_tracer("test").start_as_current_span("work") as span:
            span.set_attribute("app.account_number", SECRET)
        provider.force_flush()
        (exported,) = memory.get_finished_spans()
        assert "app.account_number" not in exported.attributes

    def test_unparsable_url_is_dropped_at_low(self):
        exported = _export_at("low", {"url.full": "http://[::1/x", "http.method": "GET"})
        assert dict(exported.attributes) == {"http.method": "GET"}


class _FailingExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector unreachable")


def _one_raw_span():
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("test").start_as_current_span("work") as span:
        span.set_status(Status(StatusCode.ERROR, SECRET))
    return memory.get_finished_spans()


class TestExportFailureSafety:
    """Instrumentation never fails the unit of work."""

    def test_production_drops_the_batch_and_warns_once(self, caplog):
        exporter = RedactingSpanExporter(_FailingExporter())
        spans = _one_raw_span()
        with caplog.at_level(logging.WARNING, logger="auditry.otel"):
            first = exporter.export(spans)
            second = exporter.export(spans)
        assert first is second
        assert first.name == "FAILURE"
        assert [r.getMessage() for r in caplog.records] == ["span export failed; dropping batch"]

    def test_strict_mode_raises(self):
        _set_strict(True)
        with pytest.raises(RuntimeError, match="collector unreachable"):
            RedactingSpanExporter(_FailingExporter()).export(_one_raw_span())


# ---------------------------------------------------------------------------
# Provider setup
# ---------------------------------------------------------------------------


class TestProviderSetup:
    def test_resource_carries_service_identity(self):
        provider = build_tracer_provider(
            service="svc", version="1.2.3", environment="sandbox", exporter=InMemorySpanExporter()
        )
        attributes = provider.resource.attributes
        assert attributes["service.name"] == "svc"
        assert attributes["service.version"] == "1.2.3"
        assert attributes["deployment.environment.name"] == "sandbox"

    def test_resource_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("SERVICE_NAME", "env-svc")
        monkeypatch.setenv("ENVIRONMENT", "dev")
        provider = build_tracer_provider(exporter=InMemorySpanExporter())
        assert provider.resource.attributes["service.name"] == "env-svc"
        assert provider.resource.attributes["deployment.environment.name"] == "dev"

    def test_configure_tracing_installs_the_global_provider(self, monkeypatch):
        installed = []
        monkeypatch.setattr(auditry_tracing.otel_trace, "set_tracer_provider", installed.append)
        provider = configure_tracing(service="svc", exporter=InMemorySpanExporter())
        assert installed == [provider]

    def test_configure_tracing_refuses_to_replace_an_installed_provider(self, monkeypatch):
        installed = TracerProvider()
        monkeypatch.setattr(auditry_tracing.otel_trace, "get_tracer_provider", lambda: installed)
        with pytest.raises(RuntimeError, match="already installed"):
            configure_tracing(exporter=InMemorySpanExporter())


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------


class TestPropagation:
    def test_outbound_headers_carry_traceparent_inside_a_span(self, tracing):
        with tracing.tracer.start_as_current_span("call") as span:
            headers = outbound_headers()
        trace_id = otel_trace.format_trace_id(span.get_span_context().trace_id)
        assert headers["traceparent"].split("-")[1] == trace_id
        assert "X-Request-ID" in headers

    def test_no_traceparent_outside_a_span(self):
        assert "traceparent" not in outbound_headers()
        assert trace_context() == {}

    def test_api_alone_does_not_count_as_the_extra(self, monkeypatch):
        monkeypatch.setattr(
            auditry_otel_context,
            "_OTEL_EXTRA_MODULES",
            ("opentelemetry.trace", "opentelemetry.not_installed"),
        )
        assert not auditry_otel_context.otel_extra_installed()

    def test_sqs_attributes_carry_only_the_correlation_id(self, tracing):
        with tracing.tracer.start_as_current_span("produce"):
            attrs = sqs_message_attributes()
        assert list(attrs) == ["correlation_id"]

    def test_with_correlation_continues_the_producer_trace_and_restores(self):
        @with_correlation
        def task():
            return current_trace_ids()

        assert task(correlation_id="c1", traceparent=TRACEPARENT)[0] == TRACE_ID
        assert current_trace_ids() is None

    def test_malformed_traceparent_keeps_the_current_trace(self, tracing):
        @with_correlation
        def task():
            return current_trace_ids()

        with tracing.tracer.start_as_current_span("outer") as span:
            assert task(traceparent="garbage")[0] == _hex_trace_id(span)

    def test_with_correlation_keeps_trace_kwargs_the_task_accepts(self):
        @with_correlation
        def task(traceparent=None):
            return traceparent

        assert task(traceparent=TRACEPARENT) == TRACEPARENT

    @pytest.mark.asyncio
    async def test_with_correlation_async(self):
        @with_correlation
        async def task():
            return current_trace_ids()

        assert (await task(traceparent=TRACEPARENT))[0] == TRACE_ID


# ---------------------------------------------------------------------------
# Worker spans
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_tracing(tracing, monkeypatch):
    """Route auditry's consumer spans to the test provider instead of the
    process-global one, which OpenTelemetry allows setting only once."""
    monkeypatch.setattr(
        auditry_otel_context, "_tracer", lambda: tracing.provider.get_tracer("auditry")
    )
    return tracing


def _consumer_spans(tracing):
    return [s for s in tracing.spans() if s.kind is otel_trace.SpanKind.CONSUMER]


class TestWorkerSpans:
    def test_worker_log_lines_carry_the_consumer_span(self, worker_tracing, capsys):
        @with_correlation
        def task():
            get_logger("test").info("processing")

        _capture_logs()
        task(traceparent=TRACEPARENT)
        (span,) = _consumer_spans(worker_tracing)
        line = _log_lines(capsys)[-1]
        assert line["span_id"] == otel_trace.format_span_id(span.context.span_id)

    def test_failed_task_marks_the_span_without_its_message(self, worker_tracing):
        @with_correlation
        def task():
            raise ValueError(f"customer {SECRET}")

        with pytest.raises(ValueError, match="customer"):
            task(traceparent=TRACEPARENT)
        (span,) = _consumer_spans(worker_tracing)
        assert span.status.status_code is StatusCode.ERROR
        assert SECRET not in span.to_json()

    def test_with_correlation_task_span_continues_the_producer_trace(self, worker_tracing):
        @with_correlation
        def send_email():
            return None

        send_email(correlation_id="c1", traceparent=TRACEPARENT)
        (span,) = _consumer_spans(worker_tracing)
        assert span.name == "process send_email"
        assert _hex_trace_id(span) == TRACE_ID
        assert otel_trace.format_span_id(span.parent.span_id) == PARENT_SPAN_ID
        assert span.attributes["correlation_id"] == "c1"

    @pytest.mark.asyncio
    async def test_with_correlation_async_task_span(self, worker_tracing):
        @with_correlation
        async def resize_image():
            return None

        await resize_image(traceparent=TRACEPARENT)
        (span,) = _consumer_spans(worker_tracing)
        assert span.name == "process resize_image"
        assert _hex_trace_id(span) == TRACE_ID
