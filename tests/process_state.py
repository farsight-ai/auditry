import logging

import structlog
from asgi_correlation_id import correlation_id

from auditry.logging_config import _set_config_service, _set_strict, set_trace_handler
from auditry.propagation import _set_correlation_header
from auditry.tracing import _reset_warnings


def reset_process_state() -> None:
    """Production policy, nothing bound, nothing configured, nothing warned about."""
    correlation_id.set(None)
    structlog.contextvars.clear_contextvars()
    set_trace_handler(None)
    _set_config_service(None)
    _set_correlation_header(None)
    _set_strict(False)
    _reset_warnings()
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
