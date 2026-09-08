"""
Auditry - Observability middleware for Python web frameworks.

This package provides comprehensive request/response logging, request ID tracking,
business event extraction, and sensitive data redaction for FastAPI and Quart applications.

Basic Usage:

    FastAPI:
    ```python
    from fastapi import FastAPI
    from auditry.fastapi import create_middleware
    from auditry import ObservabilityConfig

    app = FastAPI()
    app = create_middleware(app, ObservabilityConfig(service_name="my-api"))
    ```

    Quart:
    ```python
    from quart import Quart
    from auditry.quart import create_middleware
    from auditry import ObservabilityConfig

    app = Quart(__name__)
    app = create_middleware(app, ObservabilityConfig(service_name="my-api"))
    ```
"""

from .correlation import get_correlation_id
from .logging_config import configure_logging, get_logger, is_strict, set_trace_handler
from .metrics import ForbiddenDimensionError, MetricsLogger
from .models import BusinessEventConfig, ObservabilityConfig
from .propagation import (
    bind_correlation_id,
    bind_from_sqs_message,
    bound_correlation_id,
    ensure_correlation_id,
    outbound_headers,
    sqs_message_attributes,
    with_correlation,
)

__version__ = "0.4.0"

__all__ = [
    # Configuration
    "ObservabilityConfig",
    "BusinessEventConfig",
    # Logging
    "configure_logging",
    "get_logger",
    "set_trace_handler",
    "is_strict",
    # Correlation & propagation
    "get_correlation_id",
    "bind_correlation_id",
    "bound_correlation_id",
    "ensure_correlation_id",
    "outbound_headers",
    "sqs_message_attributes",
    "bind_from_sqs_message",
    "with_correlation",
    # Metrics
    "MetricsLogger",
    "ForbiddenDimensionError",
]
