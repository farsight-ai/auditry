"""Tests for auditry_otel.ScrubbingSpanExporter and scrub_span against real SDK spans."""

import importlib.metadata

import pytest

from auditry import configure_logging
from auditry.span_scrubbing import SpanScrubPolicy, set_span_scrub_policy

sdk = pytest.importorskip("opentelemetry.sdk.trace")
sdk_export = pytest.importorskip("opentelemetry.sdk.trace.export")
in_memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
trace = pytest.importorskip("opentelemetry.trace")
auditry_otel = pytest.importorskip("auditry.otel")

pytestmark = pytest.mark.usefixtures("clean_logging")

R = "[REDACTED]"
MESSAGE = "cannot open q3.pdf for jane@example.com"


def only(spans):
    (span,) = spans
    return span


def raise_inside_span(tracer, **kwargs):
    with pytest.raises(ValueError, match="cannot open"):
        with tracer.start_as_current_span("work", **kwargs):
            raise ValueError(MESSAGE)


class TestExceptions:
    def test_event_keeps_only_type_and_escaped(self, otel):
        raise_inside_span(otel.tracer)
        raw_event = only(only(otel.raw_spans()).events)
        assert raw_event.attributes["exception.message"] == MESSAGE
        assert "exception.stacktrace" in raw_event.attributes

        scrubbed = only(otel.scrubbed_spans())
        assert dict(only(scrubbed.events).attributes) == {
            "exception.type": "ValueError",
            "exception.escaped": "False",
        }

    def test_status_description_becomes_the_exception_type(self, otel):
        raise_inside_span(otel.tracer)
        assert only(otel.raw_spans()).status.description == f"ValueError: {MESSAGE}"
        status = only(otel.scrubbed_spans()).status
        assert status.status_code is trace.StatusCode.ERROR
        assert status.description == "ValueError"

    def test_status_description_uses_the_scrubbed_exception_type(self, otel):
        with otel.tracer.start_as_current_span("work") as span:
            span.add_event("exception", {"exception.type": "Denied for jane@example.com"})
            span.set_status(trace.Status(trace.StatusCode.ERROR, MESSAGE))
        scrubbed = only(otel.scrubbed_spans())
        assert scrubbed.status.description == f"Denied for {R}"
        assert "jane@example.com" not in scrubbed.to_json()

    def test_status_description_without_an_exception_event(self, otel):
        with otel.tracer.start_as_current_span("work") as span:
            span.set_status(trace.Status(trace.StatusCode.ERROR, MESSAGE))
        status = only(otel.scrubbed_spans()).status
        assert status.status_code is trace.StatusCode.ERROR
        assert status.description is None

    def test_no_detail_anywhere_in_the_exported_span(self, otel):
        raise_inside_span(otel.tracer)
        exported = only(otel.scrubbed_spans()).to_json()
        assert "q3.pdf" not in exported
        assert "jane@example.com" not in exported
        assert "Traceback" not in exported


class TestServerSpan:
    ATTRIBUTES = {
        "http.request.method": "GET",
        "http.route": "/files/{name}",
        "url.path": "/files/q3-board-deck.pdf",
        "url.query": "email=jane@example.com&token=abc&page=2",
        "http.target": "/files/q3-board-deck.pdf?email=jane@example.com&token=abc&page=2",
        "http.url": "http://u:p@api/files/q3-board-deck.pdf?email=jane@example.com&page=2",
        "http.response.status_code": 200,
        "client.address": "203.0.113.7",
        "net.peer.ip": "203.0.113.7",
        "http.request.header.authorization": ("Bearer abc",),
        "http.request.header.x_api_key": ("abc",),
        "http.request.header.accept": ("application/json",),
        "enduser.id": "u-1",
        "user.email": "jane@example.com",
        "user_agent.original": "curl/8",
        "correlation_id": "0f0e0d0c0b0a09080706050403020100",
    }

    def export(self, otel, name="GET /files/q3-board-deck.pdf", **attributes):
        with otel.tracer.start_as_current_span(
            name, kind=trace.SpanKind.SERVER, attributes={**self.ATTRIBUTES, **attributes}
        ) as span:
            span.add_event("gen_ai.choice", {"message": "hello"})
            span.add_event("note", {"by": "jane@example.com"})
        return only(otel.scrubbed_spans())

    def test_every_rule_applies(self, otel):
        set_span_scrub_policy(SpanScrubPolicy(query_param_allowlist=frozenset({"page"})))
        span = self.export(otel)
        assert span.name == "GET /files/{name}"
        assert dict(span.attributes) == {
            "http.request.method": "GET",
            "http.route": "/files/{name}",
            "url.path": "/files/{name}",
            "url.query": f"email={R}&token={R}&page=2",
            "http.target": f"/files/{{name}}?email={R}&token={R}&page=2",
            "http.url": f"http://api/files/{{name}}?email={R}&page=2",
            "http.response.status_code": 200,
            "http.request.header.authorization": (R,),
            "http.request.header.x_api_key": (R,),
            "http.request.header.accept": ("application/json",),
            "user_agent.original": "curl/8",
            "correlation_id": "0f0e0d0c0b0a09080706050403020100",
        }
        assert [(event.name, dict(event.attributes)) for event in span.events] == [
            ("note", {"by": R})
        ]

    def test_empty_allowlist_redacts_every_value(self, otel):
        span = self.export(otel)
        assert span.attributes["url.query"] == f"email={R}&token={R}&page={R}"

    def test_keep_client_address(self, otel):
        set_span_scrub_policy(SpanScrubPolicy(keep_client_address=True))
        span = self.export(otel)
        assert span.attributes["client.address"] == "203.0.113.7"
        assert span.attributes["net.peer.ip"] == "203.0.113.7"

    def test_unrouted_server_span_is_named_by_method(self, otel):
        span = self.export(otel, name="GET /nope", **{"http.route": None})
        assert span.name == "GET"

    def test_identity_and_timing_survive(self, otel):
        with otel.tracer.start_as_current_span("work", kind=trace.SpanKind.SERVER):
            pass
        raw, scrubbed = only(otel.raw_spans()), only(otel.scrubbed_spans())
        assert scrubbed.context == raw.context
        assert scrubbed.parent == raw.parent
        assert (scrubbed.start_time, scrubbed.end_time) == (raw.start_time, raw.end_time)
        assert scrubbed.kind is trace.SpanKind.SERVER
        assert scrubbed.resource == raw.resource
        assert scrubbed.instrumentation_scope == raw.instrumentation_scope


class TestClientAndInternalSpans:
    def test_client_span_keeps_peer_address_and_path(self, otel):
        attributes = {
            "http.request.method": "GET",
            "url.full": "https://api.example.com/v1/files/a.pdf?sig=abc",
            "server.address": "api.example.com",
            "network.peer.address": "198.51.100.1",
        }
        with otel.tracer.start_as_current_span(
            "GET", kind=trace.SpanKind.CLIENT, attributes=attributes
        ):
            pass
        assert dict(only(otel.scrubbed_spans()).attributes) == {
            **attributes,
            "url.full": f"https://api.example.com/v1/files/a.pdf?sig={R}",
        }

    def test_db_and_genai(self, otel):
        attributes = {
            "db.system": "postgresql",
            "db.operation.name": "SELECT",
            "db.query.text": "SELECT * FROM t WHERE email = 'jane@x.com' AND age > 30",
            "db.query.parameter.0": "jane",
            "gen_ai.request.model": "claude-opus-5-5",
            "gen_ai.input.messages": "[]",
        }
        with otel.tracer.start_as_current_span(
            "q", kind=trace.SpanKind.CLIENT, attributes=attributes
        ):
            pass
        assert dict(only(otel.scrubbed_spans()).attributes) == {
            "db.system": "postgresql",
            "db.operation.name": "SELECT",
            "db.query.text": "SELECT * FROM t WHERE email = ? AND age > ?",
            "gen_ai.request.model": "claude-opus-5-5",
        }

    def test_link_attributes_are_scrubbed(self, otel):
        with otel.tracer.start_as_current_span("producer") as producer:
            pass
        link = trace.Link(
            producer.get_span_context(), {"user.id": "u-1", "note": "jane@example.com"}
        )
        with otel.tracer.start_as_current_span("consumer", links=[link]):
            pass
        consumer = otel.scrubbed_spans()[-1]
        assert dict(only(consumer.links).attributes) == {"note": R}

    def test_name_is_cut_at_the_query(self, otel):
        with otel.tracer.start_as_current_span("GET /a?token=abc"):
            pass
        assert only(otel.scrubbed_spans()).name == "GET /a"


class TestPolicySource:
    def test_registry_is_read_at_export_time(self, otel):
        set_span_scrub_policy(SpanScrubPolicy(keep_client_address=True))
        with otel.tracer.start_as_current_span(
            "s", kind=trace.SpanKind.SERVER, attributes={"client.address": "203.0.113.7"}
        ):
            pass
        assert only(otel.scrubbed_spans()).attributes["client.address"] == "203.0.113.7"

    def test_explicit_policy_wins(self):
        inner = in_memory.InMemorySpanExporter()
        provider = sdk.TracerProvider()
        provider.add_span_processor(
            sdk_export.SimpleSpanProcessor(
                auditry_otel.ScrubbingSpanExporter(inner, SpanScrubPolicy())
            )
        )
        set_span_scrub_policy(SpanScrubPolicy(keep_client_address=True))
        try:
            with provider.get_tracer("t").start_as_current_span(
                "s", kind=trace.SpanKind.SERVER, attributes={"client.address": "203.0.113.7"}
            ):
                pass
        finally:
            set_span_scrub_policy(None)
        assert "client.address" not in only(inner.get_finished_spans()).attributes


class TestFailClosed:
    @pytest.fixture
    def exporter(self, monkeypatch):
        module = auditry_otel.exporter
        real = module.scrub_span

        def scrub_or_explode(span, policy):
            if span.name == "bad":
                raise RuntimeError("value jane@example.com must not be logged")
            return real(span, policy)

        monkeypatch.setattr(module, "scrub_span", scrub_or_explode)
        inner = in_memory.InMemorySpanExporter()
        provider = sdk.TracerProvider()
        scrubbing = auditry_otel.ScrubbingSpanExporter(inner)
        provider.add_span_processor(
            sdk_export.SimpleSpanProcessor(in_memory.InMemorySpanExporter())
        )
        return provider.get_tracer("t"), scrubbing, inner

    @staticmethod
    def finished(tracer, *names):
        spans = []
        for name in names:
            with tracer.start_as_current_span(name) as span:
                pass
            spans.append(span)
        return spans

    def test_failing_span_is_dropped_and_the_rest_exported(self, exporter, log_lines):
        configure_logging(service="svc", environment="prod")
        tracer, scrubbing, inner = exporter
        batch = self.finished(tracer, "bad", "good")
        assert scrubbing.export(batch) is sdk_export.SpanExportResult.SUCCESS
        assert scrubbing.export(batch) is sdk_export.SpanExportResult.SUCCESS
        assert [span.name for span in inner.get_finished_spans()] == ["good", "good"]
        warnings = [line for line in log_lines() if line.get("operation") == "scrub_span"]
        assert len(warnings) == 1
        assert warnings[0]["error_type"] == "RuntimeError"
        assert "jane@example.com" not in str(warnings)

    def test_warns_once_per_exporter_instance(self, exporter, log_lines):
        configure_logging(service="svc", environment="prod")
        tracer, scrubbing, _ = exporter
        batch = self.finished(tracer, "bad")
        scrubbing.export(batch)
        auditry_otel.ScrubbingSpanExporter(in_memory.InMemorySpanExporter()).export(batch)
        warnings = [line for line in log_lines() if line.get("operation") == "scrub_span"]
        assert len(warnings) == 2

    def test_all_spans_dropped_exports_nothing(self, exporter):
        configure_logging(service="svc", environment="prod")
        tracer, scrubbing, inner = exporter
        assert scrubbing.export(self.finished(tracer, "bad")) is sdk_export.SpanExportResult.SUCCESS
        assert inner.get_finished_spans() == ()

    def test_strict_mode_raises(self, exporter):
        configure_logging(service="svc", strict=True)
        tracer, scrubbing, inner = exporter
        with pytest.raises(RuntimeError):
            scrubbing.export(self.finished(tracer, "bad", "good"))
        assert inner.get_finished_spans() == ()


def test_rebuilt_span_resets_dropped_counts():
    inner = in_memory.InMemorySpanExporter()
    raw = in_memory.InMemorySpanExporter()
    provider = sdk.TracerProvider(span_limits=sdk.SpanLimits(max_span_attributes=1))
    provider.add_span_processor(sdk_export.SimpleSpanProcessor(raw))
    provider.add_span_processor(
        sdk_export.SimpleSpanProcessor(auditry_otel.ScrubbingSpanExporter(inner))
    )
    with provider.get_tracer("t").start_as_current_span("s", attributes={"a": 1, "b": 2}):
        pass
    assert only(raw.get_finished_spans()).dropped_attributes == 1
    assert only(inner.get_finished_spans()).dropped_attributes == 0


def test_shutdown_and_force_flush_delegate():
    calls = []

    class Inner(in_memory.InMemorySpanExporter):
        def shutdown(self):
            calls.append("shutdown")

        def force_flush(self, timeout_millis=30000):
            calls.append(("force_flush", timeout_millis))
            return False

    exporter = auditry_otel.ScrubbingSpanExporter(Inner())
    assert exporter.force_flush(1234) is False
    exporter.shutdown()
    assert calls == [("force_flush", 1234), "shutdown"]


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("auditry_otlp", auditry_otel.ScrubbingOTLPSpanExporter),
        ("auditry_console", auditry_otel.ScrubbingConsoleSpanExporter),
    ],
)
def test_entry_point_resolves(name, cls):
    (entry_point,) = importlib.metadata.entry_points(
        group="opentelemetry_traces_exporter", name=name
    )
    loaded = entry_point.load()
    assert loaded is cls
    exporter = loaded()  # the SDK constructs exporters with no arguments
    assert isinstance(exporter, auditry_otel.ScrubbingSpanExporter)
    exporter.shutdown()


def test_raw_asgi_app_exception_through_opentelemetry_middleware(otel):
    asgi = pytest.importorskip("opentelemetry.instrumentation.asgi")
    testclient = pytest.importorskip("starlette.testclient")

    async def app(scope, receive, send):
        raise ValueError(MESSAGE)

    wrapped = asgi.OpenTelemetryMiddleware(app, tracer_provider=otel.provider)
    with pytest.raises(ValueError, match="cannot open"):
        testclient.TestClient(wrapped).get("/files/q3.pdf")

    (raw,) = [span for span in otel.raw_spans() if span.kind is trace.SpanKind.SERVER]
    assert MESSAGE in raw.status.description
    (scrubbed,) = [span for span in otel.scrubbed_spans() if span.kind is trace.SpanKind.SERVER]
    assert scrubbed.status.description == "ValueError"
    assert scrubbed.name == "GET"
    # No route here, so the raw path stays; the message must not.
    exported = scrubbed.to_json()
    assert "cannot open" not in exported
    assert "jane@example.com" not in exported
