"""Opt-in Quart server spans (tracing.instrument_server) and correlation_id
on spans from an app that instruments itself."""

import asyncio

import pytest

from auditry import ObservabilityConfig, configure_logging

pytest.importorskip("opentelemetry.instrumentation.asgi")
quart = pytest.importorskip("quart")
trace = pytest.importorskip("opentelemetry.trace")
asgi = pytest.importorskip("opentelemetry.instrumentation.asgi")
auditry_quart = pytest.importorskip("auditry.quart")
quart_tracing = pytest.importorskip("auditry.quart.tracing")

pytestmark = [pytest.mark.usefixtures("clean_logging", "otel"), pytest.mark.asyncio]

R = "[REDACTED]"
FILE_URL = "/files/q3-board-deck.pdf?email=jane@example.com&token=abc&page=2"
MESSAGE = "cannot open q3.pdf for jane@example.com"


def make_config(instrument_server=True, excluded_paths=None, **tracing):
    kwargs = {} if excluded_paths is None else {"excluded_paths": excluded_paths}
    return ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        tracing={"instrument_server": instrument_server, **tracing},
        **kwargs,
    )


def add_routes(app):
    @app.route("/files/<name>")
    async def get_file(name):
        return {"ok": True}

    @app.route("/health")
    async def health():
        return {"ok": True}

    @app.route("/boom")
    async def boom():
        raise ValueError(MESSAGE)

    @app.route("/stream")
    async def stream():
        @quart.stream_with_context
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.01)

        return quart.Response(generate(), 200, mimetype="text/event-stream")

    @app.route("/regular")
    async def regular():
        return {"message": "Regular response", "data": [1, 2, 3]}

    @app.route("/large")
    async def large():
        return {"data": list(range(1000))}

    return app


def traced_app(**tracing):
    return auditry_quart.create_middleware(
        add_routes(quart.Quart(__name__)), make_config(**tracing)
    )


def guarded_app(guard):
    """An app whose own before_request hook, registered before
    create_middleware, rejects every request."""
    app = add_routes(quart.Quart(__name__))
    app.before_request(guard)
    return auditry_quart.create_middleware(app, make_config())


async def body_of(response):
    return "".join([chunk.decode() async for chunk in response.response])


class TestServerSpans:
    async def test_named_by_route_rule(self, otel):
        response = await traced_app().test_client().get(FILE_URL)
        assert response.status_code == 200
        (raw,) = otel.raw_server_spans()
        assert raw.name == "GET /files/<name>"
        assert raw.attributes["http.route"] == "/files/<name>"
        (scrubbed,) = otel.scrubbed_server_spans()
        assert scrubbed.name == "GET /files/<name>"

    async def test_unmatched_request_is_named_by_method(self, otel):
        response = await traced_app().test_client().get("/nope/jane@example.com")
        assert response.status_code == 404
        (raw,) = otel.raw_server_spans()
        assert "http.route" not in raw.attributes
        (scrubbed,) = otel.scrubbed_server_spans()
        assert scrubbed.name == "GET"
        assert scrubbed.attributes["http.status_code"] == 404
        assert "jane@example.com" not in scrubbed.to_json()

    async def test_excluded_path_gets_no_span_but_keeps_the_request_id(self, otel):
        response = await traced_app().test_client().get("/health")
        assert response.status_code == 200
        assert response.headers.getlist("X-Request-ID")
        assert otel.raw_spans() == []

    async def test_method_specific_exclusion(self, otel):
        client = traced_app(excluded_paths={"GET": ["/regular"]}).test_client()
        await client.get("/regular")
        assert otel.raw_spans() == []
        await client.get(FILE_URL)
        assert len(otel.raw_server_spans()) == 1

    async def test_host_header_cannot_match_an_excluded_path(self, otel):
        client = traced_app(excluded_paths=["/stream*"]).test_client()
        response = await client.get(FILE_URL, headers={"Host": "example.com/stream"})
        assert response.status_code == 200
        (span,) = otel.raw_server_spans()
        assert span.name == "GET /files/<name>"

    @pytest.mark.parametrize("reject", ["return", "abort"])
    async def test_rejecting_app_hook_still_gets_route_and_correlation_id(self, otel, reject):
        async def guard():
            if reject == "abort":
                quart.abort(403)
            return {"error": "forbidden"}, 403

        response = await guarded_app(guard).test_client().get(FILE_URL)
        assert response.status_code == 403
        (span,) = otel.scrubbed_server_spans()
        assert span.name == "GET /files/<name>"
        assert span.attributes["http.target"] == "/files/<name>"
        assert span.attributes["correlation_id"] == response.headers.getlist("X-Request-ID")[0]
        assert "q3-board-deck" not in span.to_json()

    async def test_log_line_carries_the_span_trace_id(self, otel, log_lines):
        configure_logging(service="svc")
        await traced_app().test_client().get(FILE_URL)
        (span,) = otel.raw_server_spans()
        (line,) = [line for line in log_lines() if line["message"].startswith("Request completed")]
        assert line["trace_id"] == trace.format_trace_id(span.context.trace_id)

    async def test_correlation_id_attribute_matches_the_response_header(self, otel):
        response = await traced_app().test_client().get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        assert span.attributes["correlation_id"] == response.headers.getlist("X-Request-ID")[0]

    async def test_instrumenting_twice_gets_exactly_one_server_span(self, otel):
        app = traced_app()
        wrapped = app.asgi_app
        quart_tracing.instrument_quart(app, make_config())
        assert app.asgi_app is wrapped
        await app.test_client().get(FILE_URL)
        assert len(otel.raw_server_spans()) == 1

    async def test_instrument_server_off_creates_no_spans(self, otel):
        app = auditry_quart.create_middleware(
            add_routes(quart.Quart(__name__)), make_config(instrument_server=False)
        )
        await app.test_client().get(FILE_URL)
        assert otel.raw_spans() == []


class TestScrubbing:
    async def test_unhandled_exception_is_a_500_without_detail(self, otel):
        # Quart turns the exception into the 500 itself, so OpenTelemetry's
        # middleware never sees it.
        response = await traced_app().test_client().get("/boom")
        assert response.status_code == 500
        (raw,) = otel.raw_server_spans()
        assert "cannot open" not in raw.to_json()
        (scrubbed,) = otel.scrubbed_server_spans()
        assert scrubbed.status.status_code is trace.StatusCode.ERROR
        assert "jane@example.com" not in scrubbed.to_json()

    async def test_propagated_exception_detail_is_not_exported(self, otel):
        # In testing mode Quart re-raises, so the exception reaches
        # OpenTelemetry's middleware, which records its message and stack trace.
        app = traced_app()
        app.testing = True
        with pytest.raises(ValueError, match="cannot open"):
            await app.test_client().get("/boom")
        (raw,) = otel.raw_server_spans()
        (raw_event,) = raw.events
        assert raw_event.attributes["exception.message"] == MESSAGE
        (scrubbed,) = otel.scrubbed_server_spans()
        (event,) = scrubbed.events
        assert set(event.attributes) == {"exception.type", "exception.escaped"}
        assert scrubbed.status.status_code is trace.StatusCode.ERROR
        assert scrubbed.status.description == "ValueError"
        exported = scrubbed.to_json()
        assert "cannot open" not in exported
        assert "jane@example.com" not in exported

    async def test_query_values_redacted_except_the_allowlist(self, otel):
        await traced_app(query_param_allowlist=["page"]).test_client().get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        assert span.attributes["http.target"] == "/files/<name>"
        assert span.attributes["http.url"].endswith(f"/files/<name>?email={R}&token={R}&page=2")
        assert "q3-board-deck" not in span.to_json()

    async def test_client_address_removed(self, otel):
        await traced_app().test_client().get(FILE_URL)
        (span,) = otel.scrubbed_server_spans()
        for key in ("net.peer.ip", "net.peer.port", "client.address", "client.port"):
            assert key not in span.attributes


class TestStreamingWithTracing:
    async def test_stream(self):
        response = await traced_app().test_client().get("/stream")
        assert response.status_code == 200
        assert "data: chunk 2" in await body_of(response)
        assert "X-Request-ID" in response.headers

    async def test_regular_and_large(self):
        client = traced_app().test_client()
        assert (await (await client.get("/regular")).get_json())["data"] == [1, 2, 3]
        assert len((await (await client.get("/large")).get_json())["data"]) == 1000

    async def test_concurrent_streams_get_one_span_each(self, otel):
        client = traced_app().test_client()
        responses = await asyncio.gather(*[client.get("/stream") for _ in range(5)])
        for response in responses:
            assert "data: chunk 0" in await body_of(response)
        spans = otel.raw_server_spans()
        assert len(spans) == 5
        assert len({span.context.trace_id for span in spans}) == 5

    async def test_mixed_requests(self):
        client = traced_app().test_client()
        paths = ["/stream", "/regular", "/stream", "/large", "/stream"]
        responses = await asyncio.gather(*[client.get(path) for path in paths])
        for path, response in zip(paths, responses, strict=True):
            assert response.status_code == 200, path

    async def test_request_id_header_appears_exactly_once(self):
        client = traced_app().test_client()
        for path in ("/regular", "/health", "/stream"):
            response = await client.get(path)
            assert len(response.headers.getlist("X-Request-ID")) == 1, path


async def test_self_instrumented_app_records_correlation_id(otel):
    """Without instrument_server, a span from the service's own
    instrumentation still gets correlation_id."""
    app = auditry_quart.create_middleware(
        add_routes(quart.Quart(__name__)), make_config(instrument_server=False)
    )
    app.asgi_app = asgi.OpenTelemetryMiddleware(app.asgi_app, tracer_provider=otel.provider)
    response = await app.test_client().get(FILE_URL)
    (span,) = otel.raw_server_spans()
    assert span.attributes["correlation_id"] == response.headers.getlist("X-Request-ID")[0]
