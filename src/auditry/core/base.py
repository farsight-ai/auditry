"""
Core abstractions for the auditry observability package.

This module defines the abstract base classes that all framework adapters must implement.
The design prioritizes clarity, maintainability, and proper separation of concerns.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseRequestAdapter(ABC):
    """
    Abstract base class for extracting data from framework-specific request objects.

    Each framework adapter must implement these methods to extract request data
    in a consistent format that the logger can use.
    """

    @abstractmethod
    async def extract_method(self, request: Any) -> str:
        """Extract the HTTP method (GET, POST, etc.) from the request."""
        pass

    @abstractmethod
    async def extract_path(self, request: Any) -> str:
        """Extract the URL path from the request."""
        pass

    @abstractmethod
    async def extract_headers(self, request: Any) -> dict[str, str]:
        """Extract headers as a dictionary from the request."""
        pass

    @abstractmethod
    async def extract_query_params(self, request: Any) -> dict[str, str]:
        """Extract query parameters as a dictionary from the request."""
        pass

    @abstractmethod
    async def extract_path_params(self, request: Any) -> dict[str, str]:
        """Extract path/route parameters as a dictionary from the request."""
        pass

    @abstractmethod
    async def extract_body(self, request: Any) -> Optional[bytes]:
        """
        Extract the raw body bytes from the request.

        Returns None if the request has no body.
        The body will be parsed by the logger based on content type.
        """
        pass

    @abstractmethod
    async def extract_user_id(self, request: Any) -> Optional[str]:
        """
        Extract the user identifier from the request if available.

        Returns None if no user information is available.
        """
        pass

    async def extract_all(self, request: Any) -> dict[str, Any]:
        """
        Extract all request data into a standardized dictionary.

        This is a template method that calls all the extract_* methods
        and returns a consistent structure.
        """
        return {
            "method": await self.extract_method(request),
            "path": await self.extract_path(request),
            "headers": await self.extract_headers(request),
            "query_params": await self.extract_query_params(request),
            "path_params": await self.extract_path_params(request),
            "body": await self.extract_body(request),
            "user_id": await self.extract_user_id(request),
        }


class BaseResponseAdapter(ABC):
    """
    Abstract base class for extracting data from framework-specific response objects.

    Each framework adapter must implement these methods to extract response data
    in a consistent format that the logger can use.
    """

    @abstractmethod
    async def extract_status_code(self, response: Any) -> int:
        """Extract the HTTP status code from the response."""
        pass

    @abstractmethod
    async def extract_headers(self, response: Any) -> dict[str, str]:
        """Extract headers as a dictionary from the response."""
        pass

    @abstractmethod
    async def extract_body(self, response: Any) -> Optional[bytes]:
        """
        Extract the raw body bytes from the response.

        Returns None if the response has no body or if it's a streaming response.
        The body will be parsed by the logger based on content type.
        """
        pass

    async def extract_all(self, response: Any) -> dict[str, Any]:
        """
        Extract all response data into a standardized dictionary.

        This is a template method that calls all the extract_* methods
        and returns a consistent structure.
        """
        return {
            "status_code": await self.extract_status_code(response),
            "headers": await self.extract_headers(response),
            "body": await self.extract_body(response),
        }


class BaseMiddleware(ABC):
    """
    Abstract base class for observability middleware implementations.

    Each framework must implement this interface to integrate with their
    specific middleware system while using the shared logging logic.
    """

    @abstractmethod
    def __init__(self, app: Any, config: Any):
        """
        Initialize the middleware with the application and configuration.

        Args:
            app: The framework-specific application instance
            config: ObservabilityConfig instance
        """
        pass

    @abstractmethod
    async def process_request(self, request: Any, call_next: Any) -> Any:
        """
        Process a request through the middleware pipeline.

        This method should:
        1. Capture request data
        2. Call the next handler
        3. Capture response data
        4. Log the request/response
        5. Return the response

        Args:
            request: The framework-specific request object
            call_next: The next handler in the middleware chain

        Returns:
            The framework-specific response object
        """
        pass
