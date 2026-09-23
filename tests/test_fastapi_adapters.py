"""
Tests for the FastAPI request/response adapters.

The FastAPI middleware is raw ASGI and builds its request dict inline, so it
never calls these adapters -- they are reachable only by importing them
directly. They are still part of the package's surface, so they are covered
here rather than left to the middleware that bypasses them.
"""

import pytest
from fastapi import Request, Response

from auditry.fastapi.adapters import FastAPIRequestAdapter, FastAPIResponseAdapter


def make_request(headers=None, query_string=b"", path_params=None):
    """Build a Starlette Request from a bare ASGI scope."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/folders/42",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": query_string,
    }
    if path_params is not None:
        scope["path_params"] = path_params
    return Request(scope)


@pytest.fixture
def request_adapter():
    return FastAPIRequestAdapter()


@pytest.fixture
def response_adapter():
    return FastAPIResponseAdapter()


@pytest.mark.asyncio
async def test_extract_headers_lowercases_names(request_adapter):
    request = make_request(headers={"X-Request-ID": "abc-123", "Content-Type": "application/json"})

    headers = await request_adapter.extract_headers(request)

    # Starlette normalizes header names, so callers can index them predictably.
    assert headers == {"x-request-id": "abc-123", "content-type": "application/json"}


@pytest.mark.asyncio
async def test_extract_headers_of_a_request_without_any(request_adapter):
    assert await request_adapter.extract_headers(make_request()) == {}


@pytest.mark.asyncio
async def test_extract_query_params(request_adapter):
    request = make_request(query_string=b"page=2&sort=name")

    assert await request_adapter.extract_query_params(request) == {"page": "2", "sort": "name"}


@pytest.mark.asyncio
async def test_extract_query_params_keeps_only_the_last_repeated_key(request_adapter):
    # dict() over a multidict collapses duplicates; asserting it so the lossy
    # behavior is a decision rather than a surprise at a call site.
    request = make_request(query_string=b"tag=a&tag=b")

    assert await request_adapter.extract_query_params(request) == {"tag": "b"}


@pytest.mark.asyncio
async def test_extract_query_params_of_a_request_without_any(request_adapter):
    assert await request_adapter.extract_query_params(make_request()) == {}


@pytest.mark.asyncio
async def test_extract_path_params(request_adapter):
    request = make_request(path_params={"folder_id": "42"})

    assert await request_adapter.extract_path_params(request) == {"folder_id": "42"}


@pytest.mark.asyncio
async def test_extract_path_params_when_the_route_declares_none(request_adapter):
    # Nothing put path_params in the scope; Starlette defaults it to empty.
    assert await request_adapter.extract_path_params(make_request()) == {}


@pytest.mark.asyncio
async def test_response_extract_headers(response_adapter):
    response = Response(content="ok", status_code=200, headers={"X-Request-ID": "abc-123"})

    headers = await response_adapter.extract_headers(response)

    assert headers["x-request-id"] == "abc-123"


@pytest.mark.asyncio
async def test_response_extract_status_code(response_adapter):
    assert await response_adapter.extract_status_code(Response(status_code=204)) == 204
