"""
Quart-specific adapters for request and response data extraction.

These adapters implement the abstract base classes to extract data from
Quart request and response objects.
"""

import json
from typing import Optional

from quart import Request, Response, g
from quart.wrappers.response import IterableBody

from ..core import BaseRequestAdapter, BaseResponseAdapter


class QuartRequestAdapter(BaseRequestAdapter):
    """Adapter for extracting data from Quart requests."""

    def __init__(self):
        """Initialize the adapter with a body cache for request bodies."""
        self._body_cache: dict[int, bytes] = {}

    async def extract_method(self, request: Request) -> str:
        """Extract HTTP method from Quart request."""
        return request.method

    async def extract_path(self, request: Request) -> str:
        """Extract URL path from Quart request."""
        return request.path

    async def extract_headers(self, request: Request) -> dict[str, str]:
        """Extract headers from Quart request."""
        return dict(request.headers)

    async def extract_query_params(self, request: Request) -> dict[str, str]:
        """Extract query parameters from Quart request."""
        return dict(request.args)

    async def extract_path_params(self, request: Request) -> dict[str, str]:
        """Extract path parameters from Quart request."""
        # Quart stores path params in request.view_args
        if hasattr(request, "view_args") and request.view_args:
            return dict(request.view_args)
        return {}

    async def extract_body(self, request: Request) -> Optional[bytes]:
        """
        Extract body from Quart request.

        Uses caching to avoid reading the body multiple times.
        """
        try:
            request_id = id(request)
            if request_id not in self._body_cache:
                # Quart uses get_data() instead of body()
                self._body_cache[request_id] = await request.get_data()
            return self._body_cache[request_id]
        except Exception:
            return None

    async def extract_user_id(self, request: Request) -> Optional[str]:
        """
        Extract user ID from Quart request.

        Checks multiple locations:
        1. request.current_user (as seen in the matrix-microservice-api example)
        2. g.user (common Quart pattern)
        3. Direct request attributes
        """
        # Check request.current_user (from the example code)
        if hasattr(request, "current_user"):
            user = request.current_user
            if user:
                if hasattr(user, "id") and user.id:
                    return str(user.id)
                if hasattr(user, "user_id") and user.user_id:
                    return str(user.user_id)

        # Check g.user (common Quart pattern)
        if hasattr(g, "user"):
            user = g.user
            if user:
                if hasattr(user, "id") and user.id:
                    return str(user.id)
                if hasattr(user, "user_id") and user.user_id:
                    return str(user.user_id)

        # Check g.user_id directly
        if hasattr(g, "user_id") and g.user_id:
            return str(g.user_id)

        # Check request.user_id directly
        if hasattr(request, "user_id") and request.user_id:
            return str(request.user_id)

        return None

    def clear_cache(self, request: Request) -> None:
        """Clear cached body data for a request."""
        request_id = id(request)
        self._body_cache.pop(request_id, None)


class QuartResponseAdapter(BaseResponseAdapter):
    """Adapter for extracting data from Quart responses."""

    async def extract_status_code(self, response: Response) -> int:
        """Extract status code from Quart response."""
        if hasattr(response, "status_code"):
            return response.status_code

        # Handle tuple responses (body, status_code, headers)
        if isinstance(response, tuple):
            if len(response) >= 2 and isinstance(response[1], int):
                return response[1]

        return 200

    async def extract_headers(self, response: Response) -> dict[str, str]:
        """Extract headers from Quart response."""
        if hasattr(response, "headers"):
            return dict(response.headers)

        # Handle tuple responses (body, status_code, headers)
        if isinstance(response, tuple) and len(response) >= 3:
            headers = response[2]
            if isinstance(headers, dict):
                return headers

        return {}

    async def extract_body(self, response: Response) -> Optional[bytes]:
        """
        Extract body from Quart response.

        Returns None for streaming responses to avoid consuming the stream.
        Handles both Response objects and tuple returns.
        """
        try:
            # For Response objects
            if hasattr(response, "response"):
                # Check if the response is an IterableBody (streaming response)
                if isinstance(response.response, IterableBody):
                    # This is a streaming response, return None to avoid consuming it
                    return None

            # For Response objects with get_data method (non-streaming)
            if hasattr(response, "get_data"):
                # Only call get_data if we're sure it's not streaming
                # Double-check for streaming response attribute
                if hasattr(response, "response") and isinstance(response.response, IterableBody):
                    return None

                # Safe to get data for non-streaming responses
                data = await response.get_data()
                if isinstance(data, str):
                    return data.encode("utf-8")
                return data

            # For tuple responses (body, status_code, headers)
            if isinstance(response, tuple) and len(response) >= 1:
                body = response[0]

                # Convert various body types to bytes
                if isinstance(body, bytes):
                    return body
                elif isinstance(body, str):
                    return body.encode("utf-8")
                elif isinstance(body, dict):
                    return json.dumps(body).encode("utf-8")

            return None
        except Exception:
            return None
