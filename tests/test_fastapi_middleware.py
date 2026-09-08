"""Basic tests for FastAPI middleware functionality."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from auditry import ObservabilityConfig, configure_logging
from auditry.fastapi import create_middleware
from auditry.models import BusinessEventConfig

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
