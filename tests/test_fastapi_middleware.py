"""Basic tests for FastAPI middleware functionality."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from auditry import ObservabilityConfig, configure_logging
from auditry.fastapi import FastAPIMiddleware, create_middleware
from auditry.models import BusinessEventConfig
from tests.field_flags import FIELD_FLAGS, config_with_field_off

configure_logging(level="INFO")


def test_correlation_id_in_response():
    """Test that correlation ID is added to response headers."""
    app = FastAPI()
    app = create_middleware(
        app,
        config=ObservabilityConfig(service_name="test-service"),
    )

    @app.get("/test")
    def test_route():
        return {"message": "ok"}

    client = TestClient(app)
    response = client.get("/test")
    assert "X-Request-ID" in response.headers


def test_response_body_logged_when_enabled():
    """Non-streaming response body is passed to log_success when log_response_body=True."""
    app = FastAPI()
    app = create_middleware(
        app,
        config=ObservabilityConfig(
            service_name="test-service",
            log_response_body=True,
        ),
    )

    @app.get("/hello")
    def hello():
        return {"greeting": "world"}

    logged_response = {}

    with patch(
        "auditry.fastapi.middleware.RequestResponseLogger.log_success",
        side_effect=lambda **kwargs: logged_response.update(kwargs),
    ):
        client = TestClient(app)
        client.get("/hello")

    body = logged_response["response_data"].get("body")
    assert body is not None
    assert body["greeting"] == "world"


@pytest.mark.skip(
    reason="Streaming + ASGI request-body buffering deadlocks TestClient (pre-existing)"
)
def test_response_body_none_for_streaming():
    """Streaming (text/event-stream) response body is not buffered."""
    app = FastAPI()
    app = create_middleware(
        app,
        config=ObservabilityConfig(
            service_name="test-service",
            log_response_body=True,
        ),
    )

    @app.get("/stream")
    def stream():
        def generate():
            yield "data: hello\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    logged_response = {}

    with patch(
        "auditry.fastapi.middleware.RequestResponseLogger.log_success",
        side_effect=lambda **kwargs: logged_response.update(kwargs),
    ):
        client = TestClient(app)
        client.get("/stream")

    body = logged_response["response_data"].get("body")
    assert body is None


def test_response_body_not_logged_when_disabled():
    """Response body is marked disabled when log_response_body=False."""
    app = FastAPI()
    app = create_middleware(
        app,
        config=ObservabilityConfig(
            service_name="test-service",
            log_response_body=False,
        ),
    )

    @app.get("/hello")
    def hello():
        return {"greeting": "world"}

    logged_response = {}

    with patch(
        "auditry.fastapi.middleware.RequestResponseLogger.log_success",
        side_effect=lambda **kwargs: logged_response.update(kwargs),
    ):
        client = TestClient(app)
        client.get("/hello")

    body = logged_response["response_data"].get("body")
    assert body == "[BODY_LOGGING_DISABLED]"


def bodies_off_config():
    return ObservabilityConfig(
        service_name="test-service", log_request_body=False, log_response_body=False
    )


CUSTOMER_TEXT = b'{"prompt": "customer text"}'


def echo_app(config):
    """The route reads the request body and returns one."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        return {"received": len(await request.body())}

    return create_middleware(app, config)


def logged_success(app, make_request):
    """The keyword arguments log_success got for the request ``make_request`` sends."""
    logged = {}

    with patch(
        "auditry.fastapi.middleware.RequestResponseLogger.log_success",
        side_effect=lambda **kwargs: logged.update(kwargs),
    ):
        make_request(TestClient(app))

    return logged


@pytest.mark.parametrize(("flag", "side", "field"), FIELD_FLAGS)
def test_a_field_logged_off_never_reaches_the_logger(handed_to_logger, flag, side, field):
    client = TestClient(echo_app(config_with_field_off(flag)))

    client.post("/echo", params={"q": "customer text"}, content=CUSTOMER_TEXT)

    (raw_data,) = handed_to_logger[side]
    assert raw_data[field] is None


def test_disabled_request_body_is_marked_in_the_log():
    logged = logged_success(
        echo_app(bodies_off_config()), lambda client: client.post("/echo", content=CUSTOMER_TEXT)
    )

    assert logged["request_data"]["body"] == "[BODY_LOGGING_DISABLED]"


def test_disabled_streamed_response_body_is_marked_in_the_log():
    """A streamed body declares no Content-Length; the chunks sent show there was one."""
    app = FastAPI()

    @app.get("/report.csv")
    def report():
        return StreamingResponse(iter([b"a,b\n"]), media_type="text/csv")

    logged = logged_success(
        create_middleware(app, bodies_off_config()), lambda client: client.get("/report.csv")
    )

    assert logged["response_data"]["body"] == "[BODY_LOGGING_DISABLED]"


@pytest.mark.asyncio
async def test_disabled_request_body_reaches_the_app_as_the_server_sent_it():
    """Unbuffered: the app reads each chunk from the server's own receive."""
    chunks = [
        {"type": "http.request", "body": b"first", "more_body": True},
        {"type": "http.request", "body": b"second", "more_body": False},
    ]
    from_server = iter(chunks)
    app_received = []

    async def receive():
        return next(from_server, {"type": "http.disconnect"})

    async def send(message):
        pass

    async def app(scope, receive, send):
        app_received.extend([await receive(), await receive()])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [],
        "query_string": b"",
    }
    middleware = FastAPIMiddleware(app, bodies_off_config())

    await middleware(scope, receive, send)

    assert app_received == chunks


def test_business_event_extracted_from_response_body():
    """BusinessEventConfig.extract_from_response receives the buffered body."""
    app = FastAPI()
    app = create_middleware(
        app,
        config=ObservabilityConfig(
            service_name="test-service",
            log_response_body=True,
            business_events={
                "POST /orders": BusinessEventConfig(
                    event_type="order.created",
                    extract_from_response=["order_id"],
                ),
            },
        ),
    )

    @app.post("/orders")
    def create_order():
        return {"order_id": "abc-123", "status": "created"}

    logged_kwargs = {}

    with patch(
        "auditry.fastapi.middleware.RequestResponseLogger.log_success",
        side_effect=lambda **kwargs: logged_kwargs.update(kwargs),
    ):
        client = TestClient(app)
        client.post("/orders")

    # The response_data passed to log_success should contain the parsed body
    # so that _extract_business_context can extract "order_id" from it
    body = logged_kwargs["response_data"].get("body")
    assert body is not None
    assert body["order_id"] == "abc-123"


def test_create_middleware_seeds_service_identity():
    """ObservabilityConfig.service_name becomes the process-wide ``service``
    for every log line, not just the middleware's own."""
    from auditry import logging_config

    logging_config._set_config_service(None)
    try:
        create_middleware(
            FastAPI(),
            config=ObservabilityConfig(
                service_name="seeded-service",
                log_request_body=False,
                log_response_body=False,
            ),
        )
        assert logging_config._config_service == "seeded-service"
    finally:
        logging_config._set_config_service(None)
