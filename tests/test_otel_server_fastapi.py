"""Opt-in FastAPI server spans (tracing.instrument_server) and correlation_id
on spans from an app that instruments itself."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap

import pytest

from auditry import ObservabilityConfig, configure_logging

pytest.importorskip("opentelemetry.instrumentation.fastapi")
fastapi = pytest.importorskip("fastapi")
responses = pytest.importorskip("fastapi.responses")
testclient = pytest.importorskip("fastapi.testclient")
trace = pytest.importorskip("opentelemetry.trace")
instrumentation = pytest.importorskip("opentelemetry.instrumentation.fastapi")
auditry_fastapi = pytest.importorskip("auditry.fastapi")
fastapi_tracing = pytest.importorskip("auditry.fastapi.tracing")

pytestmark = pytest.mark.usefixtures("clean_logging", "otel")

R = "[REDACTED]"
FILE_URL = "/files/q3-board-deck.pdf?email=jane@example.com&token=abc&page=2"
MESSAGE = "cannot open q3.pdf for jane@example.com"


def make_config(excluded_paths=None, **tracing):
    kwargs = {} if excluded_paths is None else {"excluded_paths": excluded_paths}
    return ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        tracing={"instrument_server": True, **tracing},
        **kwargs,
    )


def add_routes(app):
    @app.get("/files/{name}")
    async def get_file(name: str):
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise ValueError(MESSAGE)

    @app.get("/stream")
    async def stream():
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.01)

        return responses.StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/sync-stream")
    def sync_stream():
        def generate():
            for i in range(3):
                yield f"data: sync chunk {i}\n\n"

        return responses.StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/report")
    async def report():
        async def generate():
            for i in range(5):
                yield f"chunk {i}\n".encode()

        return responses.StreamingResponse(generate(), media_type="application/octet-stream")

    return app


def traced_client(root_path="", **config):
    app = add_routes(fastapi.FastAPI())
    app = auditry_fastapi.create_middleware(app, make_config(**config))
    return testclient.TestClient(app, raise_server_exceptions=False, root_path=root_path)


def request_log_lines(log_lines):
    return [line for line in log_lines() if line["message"].startswith("Request completed")]


def trace_id_of(span):
    return trace.format_trace_id(span.context.trace_id)


class TestServerSpans:
    def test_named_by_route(self, otel):
        response = traced_client().get(FILE_URL)
        assert response.status_code == 200
        (raw,) = otel.raw_server_spans()
        assert raw.name == "GET /files/{name}"
        assert raw.attributes["http.route"] == "/files/{name}"
        (scrubbed,) = otel.scrubbed_server_spans()
        assert scrubbed.name == "GET /files/{name}"

    def test_no_receive_or_send_child_spans(self, otel):
        traced_client().get("/stream")
        assert len(otel.raw_spans()) == 1

    def test_excluded_path_gets_no_span_but_keeps_the_request_id(self, otel):
        response = traced_client().get("/health")
        assert response.status_code == 200
        assert response.headers.get_list("x-request-id")
        assert otel.raw_spans() == []

    def test_method_specific_exclusion_skips_only_that_method(self, otel):
        client = traced_client(excluded_paths={"POST": ["/files/*"]})
        client.post("/files/q3.pdf")
        assert otel.raw_spans() == []

    def test_method_specific_exclusion_keeps_other_methods(self, otel):
        traced_client(excluded_paths={"POST": ["/files/*"]}).get(FILE_URL)
        assert len(otel.raw_server_spans()) == 1

    def test_host_header_cannot_hide_a_request_from_its_span(self, otel):
        client = traced_client(excluded_paths=["/stream*"])
        client.get(FILE_URL, headers={"Host": "example.com/stream"})
        (span,) = otel.raw_server_spans()
        assert span.name == "GET /files/{name}"

    def test_host_header_cannot_hide_a_request_from_its_log_line(self, otel, log_lines):
        configure_logging(service="svc")
        client = traced_client(excluded_paths=["/stream*"])
        client.get(FILE_URL, headers={"Host": "example.com/stream"})
        (line,) = request_log_lines(log_lines)
        assert line["message"].startswith("Request completed: GET /files/q3-board-deck.pdf ")

    def test_excluded_path_under_a_root_path_gets_no_span(self, otel):
        traced_client(root_path="/svc").get("/svc/health")
        assert otel.raw_spans() == []

    def test_log_line_carries_the_span_trace_id(self, otel, log_lines):
        configure_logging(service="svc")
        traced_client().get(FILE_URL)
        (span,) = otel.raw_server_spans()
        (line,) = [line for line in log_lines() if line["message"].startswith("Request completed")]
        assert line["trace_id"] == trace_id_of(span)

    def test_correlation_id_attribute_matches_the_response_header(self, otel):
        response = traced_client().get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        assert span.attributes["correlation_id"] == response.headers.get_list("x-request-id")[0]

    def test_already_instrumented_app_gets_exactly_one_server_span(self, otel):
        app = add_routes(fastapi.FastAPI())
        instrumentation.FastAPIInstrumentor.instrument_app(app, tracer_provider=otel.provider)
        app = auditry_fastapi.create_middleware(app, make_config())
        testclient.TestClient(app).get(FILE_URL)
        assert len(otel.raw_server_spans()) == 1

    def test_instrument_server_off_creates_no_spans(self, otel):
        app = add_routes(fastapi.FastAPI())
        config = ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False
        )
        testclient.TestClient(auditry_fastapi.create_middleware(app, config)).get(FILE_URL)
        assert otel.raw_spans() == []


class TestUnexpectedStack:
    """An instrumentor whose stack has no OpenTelemetryMiddleware where
    instrument_fastapi expects it."""

    @pytest.fixture(autouse=True)
    def stack_without_opentelemetry(self, monkeypatch):
        monkeypatch.setattr(
            instrumentation.FastAPIInstrumentor,
            "instrument_app",
            staticmethod(lambda app, **kwargs: None),
        )

    def test_production_keeps_serving(self, otel):
        configure_logging(service="svc", environment="prod")
        assert traced_client().get(FILE_URL).status_code == 200

    def test_production_warns_that_exclusions_are_lost(self, otel, log_lines):
        configure_logging(service="svc", environment="prod")
        traced_client().get(FILE_URL)
        (warning,) = [line for line in log_lines() if line["message"] == "tracing signal dropped"]
        assert (warning["operation"], warning["error_type"]) == (
            "fastapi_excluded_paths",
            "UnexpectedStackError",
        )

    def test_strict_mode_raises(self, otel):
        configure_logging(service="svc", strict=True)
        app = auditry_fastapi.create_middleware(add_routes(fastapi.FastAPI()), make_config())
        with pytest.raises(fastapi_tracing.UnexpectedStackError):
            testclient.TestClient(app).get(FILE_URL)


class TestScrubbing:
    def test_exception_detail_is_not_exported(self, otel):
        response = traced_client().get("/boom")
        assert response.status_code == 500
        (raw,) = otel.raw_server_spans()
        raw_event = next(event for event in raw.events if event.name == "exception")
        assert raw_event.attributes["exception.message"] == MESSAGE
        assert "exception.stacktrace" in raw_event.attributes

        (scrubbed,) = otel.scrubbed_server_spans()
        for event in scrubbed.events:
            if event.name == "exception":
                assert set(event.attributes) == {"exception.type", "exception.escaped"}
        assert scrubbed.status.status_code is trace.StatusCode.ERROR
        assert scrubbed.status.description in ("ValueError", None)
        exported = scrubbed.to_json()
        assert "cannot open" not in exported
        assert "jane@example.com" not in exported

    def test_query_values_redacted_except_the_allowlist(self, otel):
        traced_client(query_param_allowlist=["page"]).get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        # http.target is the path only; the query is in http.url.
        assert span.attributes["http.target"] == "/files/{name}"
        assert span.attributes["http.url"] == (
            f"http://testserver/files/{{name}}?email={R}&token={R}&page=2"
        )
        assert "q3-board-deck" not in span.to_json()

    def test_empty_allowlist_redacts_every_value(self, otel):
        traced_client().get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        assert span.attributes["http.url"].endswith(f"page={R}")

    def test_client_address_removed(self, otel):
        traced_client().get(FILE_URL)
        (raw,) = otel.raw_server_spans()
        assert "net.peer.ip" in raw.attributes
        (span,) = otel.scrubbed_server_spans()
        assert "net.peer.ip" not in span.attributes
        assert "net.peer.port" not in span.attributes

    def test_client_address_kept_when_configured(self, otel):
        traced_client(keep_client_address=True).get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        assert "net.peer.ip" in span.attributes


# Runs where OTEL_SEMCONV_STABILITY_OPT_IN can still take effect: the mode is
# read once per process, at the first instrumentation.
DUP_MODE_SCRIPT = textwrap.dedent(
    """
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from auditry import ObservabilityConfig
    from auditry.fastapi import create_middleware
    from auditry.otel import ScrubbingSpanExporter

    exported = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(exported)))
    trace.set_tracer_provider(provider)

    app = FastAPI()

    @app.get("/files/{name}")
    async def get_file(name: str):
        return {"ok": True}

    config = ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        tracing={"instrument_server": True, "query_param_allowlist": ["page"]},
    )
    TestClient(create_middleware(app, config)).get(%r)
    (span,) = exported.get_finished_spans()
    print(json.dumps({"name": span.name, "attributes": dict(span.attributes)}))
    """
)


def test_http_dup_semconv_mode():
    result = subprocess.run(
        [sys.executable, "-c", DUP_MODE_SCRIPT % FILE_URL],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "OTEL_SEMCONV_STABILITY_OPT_IN": "http/dup"},
        timeout=25,
    )
    span = json.loads(result.stdout.strip().splitlines()[-1])
    attributes = span["attributes"]
    assert span["name"] == "GET /files/{name}"
    query = f"email={R}&token={R}&page=2"
    assert attributes["url.path"] == "/files/{name}"
    assert attributes["url.query"] == query
    assert attributes["http.target"] == "/files/{name}"
    assert attributes["http.url"] == f"http://testserver/files/{{name}}?{query}"
    for key in ("client.address", "client.port", "net.peer.ip", "net.peer.port"):
        assert key not in attributes
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200
    assert attributes["user_agent.original"]
    assert "q3-board-deck" not in json.dumps(span)
    assert "jane@example.com" not in json.dumps(span)


class TestStreamingWithTracing:
    def test_sse_stream(self):
        response = traced_client().get("/stream")
        assert response.status_code == 200
        assert "data: chunk 2" in response.text

    def test_sync_stream(self):
        response = traced_client().get("/sync-stream")
        assert "data: sync chunk 2" in response.text

    @pytest.mark.timeout(10, method="thread")
    def test_non_excluded_streaming_does_not_wedge_event_loop(self, otel):
        """DEV-1150 with tracing on: OTel's middleware also wraps receive,
        and replay_body must still defer to the real receive."""
        response = traced_client().get("/report")
        assert response.status_code == 200
        assert response.text == "chunk 0\nchunk 1\nchunk 2\nchunk 3\nchunk 4\n"
        assert len(otel.raw_server_spans()) == 1

    def test_request_id_header_appears_exactly_once(self):
        client = traced_client()
        for path in ("/files/a", "/health", "/stream"):
            assert len(client.get(path).headers.get_list("x-request-id")) == 1, path


def test_self_instrumented_app_records_correlation_id(otel):
    """Without instrument_server, a span from the service's own
    instrumentation still gets correlation_id."""
    app = add_routes(fastapi.FastAPI())
    config = ObservabilityConfig(
        service_name="svc", log_request_body=False, log_response_body=False
    )
    app = auditry_fastapi.create_middleware(app, config)
    instrumentation.FastAPIInstrumentor.instrument_app(app, tracer_provider=otel.provider)
    response = testclient.TestClient(app).get(FILE_URL)
    (span,) = otel.raw_server_spans()
    assert span.attributes["correlation_id"] == response.headers.get_list("x-request-id")[0]
