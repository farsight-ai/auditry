"""Basic tests for Quart middleware functionality."""

import pytest
from quart import Quart, Response, abort, request

from auditry import ObservabilityConfig, configure_logging
from auditry.quart import create_middleware
from tests.field_flags import FIELD_FLAGS, config_with_field_off

configure_logging(level="INFO")


@pytest.mark.asyncio
async def test_correlation_id_in_response():
    """Test that correlation ID is added to response headers."""
    app = Quart(__name__)
    app = create_middleware(app, config=ObservabilityConfig(service_name="test-quart-service"))

    @app.route("/test")
    async def test_route():
        return {"message": "ok"}

    client = app.test_client()
    response = await client.get("/test")
    assert response.status_code == 200
    assert len(response.headers.getlist("X-Request-ID")) == 1


@pytest.mark.asyncio
async def test_error_handling():
    """Test that errors are logged properly."""
    app = Quart(__name__)
    app = create_middleware(app, config=ObservabilityConfig(service_name="test-quart-service"))

    @app.route("/error")
    async def error_route():
        raise ValueError("Test error")

    client = app.test_client()
    response = await client.get("/error")
    assert response.status_code == 500
    assert len(response.headers.getlist("X-Request-ID")) == 1


def auditry_lines(log_lines):
    """auditry's request lines, without Quart's own exception log."""
    return [line for line in log_lines() if line["message"].startswith("Request ")]


def quiet_config(**kwargs):
    return ObservabilityConfig(
        service_name="svc", log_request_body=False, log_response_body=False, **kwargs
    )


def echo_app(config=None):
    """Body logging off unless ``config`` says otherwise; the route reads the
    request body and returns one."""
    app = create_middleware(Quart(__name__), config or quiet_config())

    @app.post("/echo")
    async def echo():
        return {"received": len(await request.get_data())}

    return app


def streaming_app(config):
    """The route streams its body and sets a cookie."""
    app = create_middleware(Quart(__name__), config)

    @app.get("/stream")
    async def stream():
        async def generate():
            yield b"data: x\n\n"

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Set-Cookie"] = "session=abc123"
        return response

    return app


CUSTOMER_TEXT = b'{"prompt": "customer text"}'
# Quart's test client sends no Content-Length of its own; a real client does.
DECLARED_BODY = {"Content-Length": str(len(CUSTOMER_TEXT))}


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "side", "field"), FIELD_FLAGS)
async def test_a_field_logged_off_never_reaches_the_logger(handed_to_logger, flag, side, field):
    client = echo_app(config_with_field_off(flag)).test_client()

    await client.post(
        "/echo",
        query_string={"q": "customer text"},
        data=CUSTOMER_TEXT,
        headers=DECLARED_BODY,
    )

    (raw_data,) = handed_to_logger[side]
    assert raw_data[field] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_streamed_response_headers_are_not_logged_when_header_logging_is_off(log_lines):
    configure_logging(service="svc", environment="prod")
    client = streaming_app(quiet_config()).test_client()

    await client.get("/stream")

    (line,) = auditry_lines(log_lines)
    assert "headers" not in line["response"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_streamed_response_headers_are_redacted(log_lines):
    configure_logging(service="svc", environment="prod")
    client = streaming_app(quiet_config(log_response_headers=True)).test_client()

    await client.get("/stream")

    (line,) = auditry_lines(log_lines)
    assert line["response"]["headers"]["Set-Cookie"] == "[REDACTED]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_disabled_request_body_is_marked_in_the_log(log_lines):
    configure_logging(service="svc", environment="prod")
    client = echo_app().test_client()

    await client.post("/echo", data=CUSTOMER_TEXT, headers=DECLARED_BODY)

    (line,) = auditry_lines(log_lines)
    assert line["request"]["body"] == "[BODY_LOGGING_DISABLED]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_disabled_response_body_is_marked_in_the_log(log_lines):
    configure_logging(service="svc", environment="prod")
    client = echo_app().test_client()

    await client.post("/echo", data=CUSTOMER_TEXT, headers=DECLARED_BODY)

    (line,) = auditry_lines(log_lines)
    assert line["response"]["body"] == "[BODY_LOGGING_DISABLED]"


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_unhandled_exception_is_logged_once_as_a_failure(log_lines):
    configure_logging(service="svc", environment="prod")
    app = create_middleware(Quart(__name__), quiet_config())

    @app.route("/error")
    async def error_route():
        raise ValueError("customer text")

    assert (await app.test_client().get("/error")).status_code == 500
    (line,) = auditry_lines(log_lines)
    assert line["message"].startswith("Request failed: GET /error")
    assert line["error_type"] == "ValueError"
    assert "customer text" not in str(line)


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
@pytest.mark.parametrize(("path", "status"), [("/nope", 404), ("/forbidden", 403)])
async def test_http_errors_keep_their_status(log_lines, path, status):
    """An HTTP error (unmatched route, abort()) is a response with its own
    status, logged like any other response."""
    configure_logging(service="svc", environment="prod")
    app = create_middleware(Quart(__name__), quiet_config())

    @app.route("/forbidden")
    async def forbidden():
        abort(403)

    response = await app.test_client().get(path)
    assert response.status_code == status
    assert len(response.headers.getlist("X-Request-ID")) == 1
    (line,) = auditry_lines(log_lines)
    assert line["message"].startswith(f"Request completed: GET {path} - Status: {status}")


def catch_all_app():
    """An app whose own errorhandler(Exception) turns every error into a JSON 500."""
    app = create_middleware(Quart(__name__), quiet_config())

    @app.errorhandler(Exception)
    async def envelope(error):
        return {"error": "internal"}, 500

    @app.route("/boom")
    async def boom():
        raise ValueError("customer text")

    return app


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_an_error_the_apps_catch_all_handles_is_logged_as_a_failure(log_lines):
    configure_logging(service="svc", environment="prod")
    await catch_all_app().test_client().get("/boom")
    (line,) = auditry_lines(log_lines)
    assert (line["message"].split(" - ")[0], line["error_type"]) == (
        "Request failed: GET /boom",
        "ValueError",
    )


@pytest.mark.asyncio
async def test_the_apps_catch_all_still_builds_the_response():
    response = await catch_all_app().test_client().get("/boom")
    assert (response.status_code, await response.get_json()) == (500, {"error": "internal"})


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_an_error_a_handler_turns_into_another_status_is_a_completed_request(log_lines):
    configure_logging(service="svc", environment="prod")
    app = create_middleware(Quart(__name__), quiet_config())

    @app.errorhandler(ValueError)
    async def teapot(error):
        return {"handled": True}, 418

    @app.route("/value")
    async def raises_value():
        raise ValueError("x")

    await app.test_client().get("/value")
    (line,) = auditry_lines(log_lines)
    assert line["message"].startswith("Request completed: GET /value - Status: 418")


@pytest.mark.asyncio
async def test_app_error_handlers_still_run():
    app = create_middleware(Quart(__name__), quiet_config())

    class TeapotError(Exception):
        pass

    @app.errorhandler(ValueError)
    async def value_error(error):
        return {"handled": "value"}, 418

    @app.errorhandler(500)
    async def server_error(error):
        return {"handled": "500"}, 500

    @app.route("/value")
    async def raises_value():
        raise ValueError("x")

    @app.route("/teapot")
    async def raises_teapot():
        raise TeapotError()

    client = app.test_client()
    value = await client.get("/value")
    assert (value.status_code, await value.get_json()) == (418, {"handled": "value"})
    unhandled = await client.get("/teapot")
    assert (unhandled.status_code, await unhandled.get_json()) == (500, {"handled": "500"})


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_logging")
async def test_excluded_path_errors_are_not_logged(log_lines):
    configure_logging(service="svc", environment="prod")
    app = create_middleware(Quart(__name__), quiet_config(excluded_paths=["/error"]))

    @app.route("/error")
    async def error_route():
        raise ValueError("x")

    assert (await app.test_client().get("/error")).status_code == 500
    assert auditry_lines(log_lines) == []
