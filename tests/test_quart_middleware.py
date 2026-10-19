"""Basic tests for Quart middleware functionality."""

import pytest
from quart import Quart, abort

from auditry import ObservabilityConfig, configure_logging
from auditry.quart import create_middleware

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
