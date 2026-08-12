"""Tests for Quart streaming response handling."""

import asyncio
import json
import pytest
from quart import Quart, Response, stream_with_context
from src.auditry import ObservabilityConfig
from src.auditry.quart import create_middleware


@pytest.fixture
def app():
    """Create a Quart app with auditry middleware."""
    app = Quart(__name__)

    # Configure auditry
    config = ObservabilityConfig(
        service_name="test-quart-streaming",
    )

    # Apply auditry middleware
    app = create_middleware(app, config)

    @app.route("/stream")
    async def streaming_endpoint():
        """Endpoint that returns streaming response."""
        @stream_with_context
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.01)

        return Response(generate(), 200, mimetype="text/event-stream")

    @app.route("/regular")
    async def regular_endpoint():
        """Endpoint that returns regular JSON response."""
        return {"message": "Regular response", "data": [1, 2, 3]}

    @app.route("/large")
    async def large_endpoint():
        """Endpoint that returns large JSON response."""
        return {"data": list(range(1000))}

    return app


@pytest.fixture
def app_without_middleware():
    """Create a Quart app without auditry middleware."""
    app = Quart(__name__)

    @app.route("/stream")
    async def streaming_endpoint():
        """Endpoint that returns streaming response."""
        @stream_with_context
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.01)

        return Response(generate(), 200, mimetype="text/event-stream")

    @app.route("/regular")
    async def regular_endpoint():
        """Endpoint that returns regular JSON response."""
        return {"message": "Regular response", "data": [1, 2, 3]}

    return app


@pytest.mark.asyncio
async def test_streaming_response_with_middleware(app, caplog):
    """Test that streaming responses work correctly with auditry middleware."""
    client = app.test_client()

    # Test streaming endpoint
    response = await client.get("/stream")
    assert response.status_code == 200
    assert response.content_type == "text/event-stream; charset=utf-8"

    # Collect all chunks
    chunks = []
    async for chunk in response.response:
        chunks.append(chunk.decode("utf-8"))

    # The test client might deliver chunks differently
    # Just verify we got all the data
    full_content = "".join(chunks)
    assert "data: chunk 0" in full_content
    assert "data: chunk 1" in full_content
    assert "data: chunk 2" in full_content

    # The streaming response should have been logged with body=None
    # Logs are printed to stdout by auditry's logger

    # Verify correlation header is set
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_regular_response_with_middleware(app, caplog):
    """Test that regular responses work correctly with auditry middleware."""
    client = app.test_client()

    # Test regular endpoint
    response = await client.get("/regular")
    assert response.status_code == 200

    data = await response.get_json()
    assert data == {"message": "Regular response", "data": [1, 2, 3]}

    # The regular response should have been logged with full body
    # Logs are printed to stdout by auditry's logger

    # Verify correlation header is set
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_streaming_without_middleware(app_without_middleware):
    """Test that streaming works without middleware (baseline test)."""
    client = app_without_middleware.test_client()

    # Test streaming endpoint
    response = await client.get("/stream")
    assert response.status_code == 200
    assert response.content_type == "text/event-stream; charset=utf-8"

    # Collect all chunks
    chunks = []
    async for chunk in response.response:
        chunks.append(chunk.decode("utf-8"))

    # The test client might deliver chunks differently
    # Just verify we got all the data
    full_content = "".join(chunks)
    assert "data: chunk 0" in full_content
    assert "data: chunk 1" in full_content
    assert "data: chunk 2" in full_content

    # No correlation header without middleware
    assert "X-Request-ID" not in response.headers


@pytest.mark.asyncio
async def test_large_response_with_middleware(app):
    """Test that large non-streaming responses are handled correctly."""
    client = app.test_client()

    # Test large response endpoint
    response = await client.get("/large")
    assert response.status_code == 200

    data = await response.get_json()
    assert "data" in data
    assert len(data["data"]) == 1000
    assert data["data"][0] == 0
    assert data["data"][999] == 999

    # Verify correlation header is set
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_multiple_concurrent_streams(app):
    """Test that multiple concurrent streaming requests work correctly."""
    client = app.test_client()

    # Start multiple streaming requests concurrently
    tasks = []
    for i in range(5):
        tasks.append(client.get("/stream"))

    responses = await asyncio.gather(*tasks)

    # Verify all responses are successful
    for response in responses:
        assert response.status_code == 200
        assert response.content_type == "text/event-stream; charset=utf-8"

        # Collect chunks from this response
        chunks = []
        async for chunk in response.response:
            chunks.append(chunk.decode("utf-8"))

        # Verify we got all the data
        full_content = "".join(chunks)
        assert "data: chunk 0" in full_content


@pytest.mark.asyncio
async def test_mixed_requests(app):
    """Test that mixed streaming and regular requests work correctly."""
    client = app.test_client()

    # Make mixed requests concurrently
    tasks = [
        client.get("/stream"),
        client.get("/regular"),
        client.get("/stream"),
        client.get("/large"),
        client.get("/stream"),
    ]

    responses = await asyncio.gather(*tasks)

    # Check streaming responses
    for i in [0, 2, 4]:
        response = responses[i]
        assert response.status_code == 200
        assert response.content_type == "text/event-stream; charset=utf-8"

        chunks = []
        async for chunk in response.response:
            chunks.append(chunk.decode("utf-8"))
        # Verify we got data
        full_content = "".join(chunks)
        assert "data: chunk 0" in full_content

    # Check regular response
    regular_response = responses[1]
    assert regular_response.status_code == 200
    data = await regular_response.get_json()
    assert data["message"] == "Regular response"

    # Check large response
    large_response = responses[3]
    assert large_response.status_code == 200
    data = await large_response.get_json()
    assert len(data["data"]) == 1000

@pytest.mark.asyncio
async def test_request_id_header_appears_exactly_once():
    """CorrelationIdMiddleware owns the response header; the observability
    hooks must not add a second copy (regular, streaming, and excluded
    paths alike)."""
    app = Quart(__name__)
    config = ObservabilityConfig(
        service_name="dedupe-svc",
        log_request_body=False,
        log_response_body=False,
        excluded_paths=["/excluded"],
    )

    @app.route("/regular")
    async def regular():
        return {"ok": True}

    @app.route("/excluded")
    async def excluded():
        return {"ok": True}

    @app.route("/stream")
    async def stream():
        @stream_with_context
        async def generate():
            yield b"data: x\n\n"
        return Response(generate(), 200, mimetype="text/event-stream")

    app = create_middleware(app, config)
    client = app.test_client()

    for path in ("/regular", "/excluded", "/stream"):
        response = await client.get(path)
        values = response.headers.getlist("X-Request-ID")
        assert len(values) == 1, f"{path}: expected exactly one X-Request-ID, got {values}"
