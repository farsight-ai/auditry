"""
Quart-specific adapters for request and response data extraction.

These adapters implement the abstract base classes to extract data from
Quart request and response objects.
"""

import json

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

    async def extract_body(self, request: Request) -> bytes | None:
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

    async def extract_user_id(self, request: Request) -> str | None:
        """
        Extract user ID from Quart request.

        Checks, in order, and takes a user object's ``id`` before its ``user_id``:
        1. request.current_user (as seen in the matrix-microservice-api example)
        2. g.user (common Quart pattern)
        3. g.user_id, then request.user_id
        """
        for user in (getattr(request, "current_user", None), getattr(g, "user", None)):
            user_id = _id_of(user)
            if user_id:
                return user_id
        for holder in (g, request):
            user_id = getattr(holder, "user_id", None)
            if user_id:
                return str(user_id)
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

    async def extract_body(self, response: Response) -> bytes | None:
        """
        Extract body from Quart response.

        Returns None for streaming responses to avoid consuming the stream.
        Handles both Response objects and tuple returns.
        """
        try:
            # A streaming body would be consumed by reading it.
            if isinstance(getattr(response, "response", None), IterableBody):
                return None
            if hasattr(response, "get_data"):
                return _as_bytes(await response.get_data())
            # A tuple response is (body, status_code, headers).
            if isinstance(response, tuple) and response:
                return _as_bytes(response[0])
            return None
        except Exception:
            return None


def _id_of(user: object) -> str | None:
    """A user object's ``id``, else its ``user_id``, as a string."""
    if not user:
        return None
    for attribute in ("id", "user_id"):
        value = getattr(user, attribute, None)
        if value:
            return str(value)
    return None


def _as_bytes(body: object) -> bytes | None:
    """A response body as bytes: text encoded as UTF-8, a dict as JSON."""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, dict):
        return json.dumps(body).encode("utf-8")
    return None
