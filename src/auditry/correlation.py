"""
Correlation ID management using asgi-correlation-id.

Provides a simple function to get the current correlation ID from context.
"""

from typing import Optional

from asgi_correlation_id import correlation_id

__all__ = ["get_correlation_id"]


def get_correlation_id() -> Optional[str]:
    """
    Get the current correlation ID from context.

    Returns:
        The correlation ID string, or None if not set
    """
    return correlation_id.get()
