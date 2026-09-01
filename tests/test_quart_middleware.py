"""Basic tests for Quart middleware functionality."""
import pytest
import asyncio
from quart import Quart, request, g
from quart.testing import QuartClient

from auditry import ObservabilityConfig, configure_logging
from auditry.quart import create_middleware


configure_logging(level="INFO")




@pytest.mark.asyncio
async def test_correlation_id_in_response():
    """Test that correlation ID is added to response headers."""
    app = Quart(__name__)
    app = create_middleware(
        app,
        config=ObservabilityConfig(service_name="test-quart-service")
    )

    @app.route("/test")
    async def test_route():
        return {"message": "ok"}

    client = app.test_client()
    response = await client.get("/test")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_error_handling():
    """Test that errors are logged properly."""
    app = Quart(__name__)
    app = create_middleware(
        app,
        config=ObservabilityConfig(service_name="test-quart-service")
    )

    @app.route("/error")
    async def error_route():
        raise ValueError("Test error")

    client = app.test_client()
    # The error should be raised and logged
    with pytest.raises(ValueError):
        response = await client.get("/error")

