"""
Correlation ID management using asgi-correlation-id.

Provides a simple function to get the current correlation ID from context.
"""

from asgi_correlation_id import correlation_id

__all__ = ["CORRELATION_ID_FIELD", "get_correlation_id"]

# The one name for the correlation ID wherever auditry records it: the log
# field, the span attribute and the SQS message attribute, so it joins them.
CORRELATION_ID_FIELD = "correlation_id"


def get_correlation_id() -> str | None:
    """
    Get the current correlation ID from context.

    Returns:
        The correlation ID string, or None if not set
    """
    return correlation_id.get()
