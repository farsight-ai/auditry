"""Path exclusion tests."""

import pytest
import asyncio
import json
from unittest.mock import patch
from quart import Quart, Response, stream_with_context
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from src.auditry import ObservabilityConfig, configure_logging
from src.auditry.quart import create_middleware as create_quart_middleware
from src.auditry.fastapi import create_middleware as create_fastapi_middleware


# caplog only sees the middleware's records once structlog routes through stdlib.
@pytest.fixture(autouse=True)
def _configured_logging():
    configure_logging(level="INFO")


# ================= Quart Tests =================

@pytest.fixture
def quart_app_with_exclusions():
    app = Quart(__name__)
    config = ObservabilityConfig(
        service_name="test-exclusion",
        excluded_paths=['/health', '/metrics', '/stream*', '/api/*/internal']
    )
    app = create_quart_middleware(app, config)

    @app.route("/health")
    async def health():
        return {"status": "ok"}

    @app.route("/metrics")
    async def metrics():
        return {"requests": 100}

    @app.route("/stream")
    async def streaming():
        @stream_with_context
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n"
                await asyncio.sleep(0.01)
        return Response(generate(), 200, mimetype="text/event-stream")

    @app.route("/api/v1/internal")
    async def internal():
        return {"internal": "data"}

    @app.route("/api/v1/public")
    async def public():
        return {"public": "data"}

    return app


@pytest.fixture
def quart_app_with_method_exclusions():
    """Create a Quart app with method-specific path exclusions."""
    app = Quart(__name__)

    # Configure with method-specific excluded paths
    config = ObservabilityConfig(
        service_name="test-method-exclusion",
        excluded_paths={
            'GET': ['/health', '/metrics'],
            'POST': ['/webhook/*'],
            '*': ['/admin/*']  # Excluded for all methods
        }
    )

    app = create_quart_middleware(app, config)

    @app.route("/health", methods=['GET', 'POST'])
    async def health():
        return {"status": "ok"}

    @app.route("/webhook/github", methods=['POST'])
    async def webhook():
        return {"received": True}

    @app.route("/admin/users")
    async def admin():
        return {"users": []}

    @app.route("/api/users")
    async def users():
        return {"users": ["user1"]}

    return app


@pytest.mark.asyncio
async def test_quart_exclusion(quart_app_with_exclusions, caplog):
    client = quart_app_with_exclusions.test_client()

    # Clear any existing logs
    caplog.clear()

    # Test excluded health endpoint
    response = await client.get("/health")
    assert response.status_code == 200

    # Should have correlation ID even though excluded
    assert "X-Request-ID" in response.headers

    # Should not have any logs for this request
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0


@pytest.mark.asyncio
async def test_quart_patterns(quart_app_with_exclusions, caplog):
    """Test wildcard and normal paths."""
    client = quart_app_with_exclusions.test_client()
    caplog.clear()

    # wildcard test
    response = await client.get("/stream")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers
    assert len([r for r in caplog.records if "Request completed" in r.getMessage()]) == 0

    # normal path should log
    response = await client.get("/api/v1/public")
    assert response.status_code == 200
    assert len([r for r in caplog.records if "Request completed" in r.getMessage()]) > 0


@pytest.mark.asyncio
async def test_quart_method_specific_exclusion(quart_app_with_method_exclusions, caplog):
    """Test that method-specific exclusions work correctly."""
    client = quart_app_with_method_exclusions.test_client()

    # GET /health should be excluded
    caplog.clear()
    response = await client.get("/health")
    assert response.status_code == 200
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0

    # POST /health should be logged (not excluded)
    caplog.clear()
    response = await client.post("/health")
    assert response.status_code == 200
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) > 0

    # POST /webhook/github should be excluded
    caplog.clear()
    response = await client.post("/webhook/github")
    assert response.status_code == 200
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0

    # Any method to /admin/* should be excluded
    caplog.clear()
    response = await client.get("/admin/users")
    assert response.status_code == 200
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0


# ================= FastAPI Tests =================

@pytest.fixture
def fastapi_app_with_exclusions():
    """Create a FastAPI app with path exclusions configured."""
    app = FastAPI()

    # Configure with excluded paths
    config = ObservabilityConfig(
        service_name="test-fastapi-exclusion",
        excluded_paths=['/health', '/metrics', '/stream*']
    )

    # Apply middleware
    app = create_fastapi_middleware(app, config)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return {"requests": 100}

    @app.get("/stream")
    async def streaming():
        async def generate():
            for i in range(3):
                yield f"data: chunk {i}\n\n".encode()
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/users")
    async def users():
        return {"users": ["user1", "user2"]}

    return app


@pytest.mark.asyncio
async def test_fastapi_excluded_path_not_logged(fastapi_app_with_exclusions, caplog):
    """Test that excluded paths are not logged in FastAPI."""
    from fastapi.testclient import TestClient

    client = TestClient(fastapi_app_with_exclusions)
    caplog.clear()

    # Test excluded health endpoint
    response = client.get("/health")
    assert response.status_code == 200

    # Should have correlation ID even though excluded
    assert "X-Request-ID" in response.headers

    # Should not have logs for this request
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0


@pytest.mark.asyncio
async def test_fastapi_streaming_exclusion(fastapi_app_with_exclusions, caplog):
    """Test that streaming endpoints can be excluded in FastAPI."""
    from fastapi.testclient import TestClient

    client = TestClient(fastapi_app_with_exclusions)
    caplog.clear()

    # Test excluded streaming endpoint
    response = client.get("/stream")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers

    # Should not have logs for this request
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) == 0


@pytest.mark.asyncio
async def test_fastapi_non_excluded_path_is_logged(fastapi_app_with_exclusions, caplog):
    """Test that non-excluded paths are still logged in FastAPI."""
    from fastapi.testclient import TestClient

    client = TestClient(fastapi_app_with_exclusions)
    caplog.clear()

    # Test non-excluded endpoint
    response = client.get("/api/users")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers

    # Should have logs for this request
    request_logs = [r for r in caplog.records if "Request completed" in r.getMessage()]
    assert len(request_logs) > 0


# ================= Path Matcher Unit Tests =================

def test_matcher_basics():
    from src.auditry.path_matcher import should_exclude_path

    # exact
    assert should_exclude_path('/health', 'GET', ['/health'])
    assert not should_exclude_path('/healthy', 'GET', ['/health'])

    # wildcards
    assert should_exclude_path('/api/v1/users', 'GET', ['/api/*'])
    assert should_exclude_path('/api/v1/internal', 'GET', ['/api/*/internal'])

    # query params ignored
    assert should_exclude_path('/health?check=true', 'GET', ['/health'])

    # prefix with /
    assert should_exclude_path('/api/v1/users', 'GET', ['/api/'])
    assert not should_exclude_path('/apis', 'GET', ['/api/'])


def test_method_specific():
    from src.auditry.path_matcher import should_exclude_path

    cfg = {'GET': ['/health'], 'POST': ['/webhook'], '*': ['/admin/*']}
    assert should_exclude_path('/health', 'GET', cfg)
    assert not should_exclude_path('/health', 'POST', cfg)
    assert should_exclude_path('/admin/users', 'DELETE', cfg)