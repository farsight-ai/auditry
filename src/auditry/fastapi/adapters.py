"""
FastAPI-specific adapters for request and response data extraction.

These adapters implement the abstract base classes to extract data from
FastAPI/Starlette request and response objects.
"""

import inspect
from typing import Optional

from fastapi import Request, Response
from starlette.responses import StreamingResponse

from ..core import BaseRequestAdapter, BaseResponseAdapter


class FastAPIRequestAdapter(BaseRequestAdapter):
    """Adapter for extracting data from FastAPI/Starlette requests."""

    def __init__(self):
        """Initialize the adapter with a body cache for request bodies."""
        self._body_cache: dict[int, bytes] = {}

    async def extract_method(self, request: Request) -> str:
        """Extract HTTP method from FastAPI request."""
        return request.method

    async def extract_path(self, request: Request) -> str:
        """Extract URL path from FastAPI request."""
        return request.url.path

    async def extract_headers(self, request: Request) -> dict[str, str]:
        """Extract headers from FastAPI request."""
        return dict(request.headers)

    async def extract_query_params(self, request: Request) -> dict[str, str]:
        """Extract query parameters from FastAPI request."""
        return dict(request.query_params)

    async def extract_path_params(self, request: Request) -> dict[str, str]:
        """Extract path parameters from FastAPI request."""
        # FastAPI stores path params in request.path_params
        if hasattr(request, "path_params"):
            return dict(request.path_params)
        return {}

    async def extract_body(self, request: Request) -> Optional[bytes]:
        """
        Extract body from FastAPI request.

        Uses caching to avoid reading the body multiple times.
        """
        try:
            request_id = id(request)
            if request_id not in self._body_cache:
                self._body_cache[request_id] = await request.body()
            return self._body_cache[request_id]
        except Exception:
            return None

    async def extract_user_id(self, request: Request) -> Optional[str]:
        """
        Extract user ID from FastAPI request state.

        Supports two common patterns:
        1. request.state.user_id - direct ID
        2. request.state.user - user object with id attribute
        """
        if hasattr(request.state, "user_id") and request.state.user_id:
            return str(request.state.user_id)

        if hasattr(request.state, "user"):
            user = request.state.user
            if hasattr(user, "id") and user.id:
                return str(user.id)
            if hasattr(user, "user_id") and user.user_id:
                return str(user.user_id)

        return None

    def clear_cache(self, request: Request) -> None:
        """Clear cached body data for a request."""
        request_id = id(request)
        self._body_cache.pop(request_id, None)


class FastAPIResponseAdapter(BaseResponseAdapter):
    """Adapter for extracting data from FastAPI/Starlette responses."""

    async def extract_status_code(self, response: Response) -> int:
        """Extract status code from FastAPI response."""
        return response.status_code

    async def extract_headers(self, response: Response) -> dict[str, str]:
        """Extract headers from FastAPI response."""
        return dict(response.headers)

    async def extract_body(self, response: Response) -> Optional[bytes]:
        """
        Extract body from FastAPI response.

        Returns None for streaming responses to avoid consuming the stream.
        """
        # Skip streaming responses
        if isinstance(response, StreamingResponse):
            return None

        # Check if response has a body_iterator (generator/async generator)
        if hasattr(response, "body_iterator"):
            # Check if it's a generator or async generator
            if inspect.isasyncgen(response.body_iterator) or inspect.isgenerator(
                response.body_iterator
            ):
                return None

        # Get body if available
        if hasattr(response, "body"):
            return response.body

        return None
