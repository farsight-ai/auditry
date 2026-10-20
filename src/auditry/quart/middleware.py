"""Quart observability middleware."""

import time

from asgi_correlation_id import CorrelationIdMiddleware
from quart import Quart, Request, Response, request
from quart.signals import got_request_exception, request_started, websocket_started
from quart.wrappers.response import IterableBody

from ..core import BaseMiddleware, RequestResponseLogger
from ..correlation import get_correlation_id
from ..logging_config import _set_config_service
from ..models import ObservabilityConfig
from ..path_matcher import should_exclude_path
from ..propagation import _set_correlation_header
from ..span_scrubbing import SpanScrubPolicy, set_span_scrub_policy
from ..tracing import require_otel, set_span_correlation_id
from .adapters import QuartRequestAdapter, QuartResponseAdapter

# Sent for each request or websocket before any before_request /
# before_websocket hook runs, so an app hook that returns a response or
# aborts cannot skip a receiver. Both are process-wide: connect with sender=.
CONNECTION_STARTED_SIGNALS = (request_started, websocket_started)


class QuartMiddleware(BaseMiddleware):
    """Quart middleware with logging and correlation."""

    def __init__(self, app: Quart, config: ObservabilityConfig):
        self.app = app
        self.config = config
        self.logger = RequestResponseLogger(config)
        self.request_adapter = QuartRequestAdapter()
        self.response_adapter = QuartResponseAdapter()

        self._setup_correlation_middleware()
        self._register_hooks()

    def _setup_correlation_middleware(self):
        """Setup correlation ID middleware."""
        original_asgi = self.app.asgi_app
        correlation_app = CorrelationIdMiddleware(
            app=original_asgi,
            header_name=self.config.correlation_id_header,
        )
        self.app.asgi_app = correlation_app

    def _register_hooks(self):
        """Register Quart before_request and after_request hooks, and the
        connection-started and unhandled-exception receivers."""

        for signal in CONNECTION_STARTED_SIGNALS:
            signal.connect(_record_span_correlation_id, sender=self.app)

        @self.app.before_request
        async def capture_request_start():
            """Hook for request start."""
            # Early exit for excluded paths
            if should_exclude_path(request.path, request.method, self.config.excluded_paths):
                request.observability_excluded = True
                request.observability_correlation_id = get_correlation_id()
                return

            request.observability_start_time = time.time()
            request.observability_correlation_id = get_correlation_id()
            request.observability_excluded = False

            # extract and cache request body early
            raw_request_data = await self.request_adapter.extract_all(request)
            request.observability_raw_data = raw_request_data

            request.observability_request_data = self.logger.prepare_request_data(
                raw_request_data, request.observability_correlation_id
            )

        @self.app.after_request
        async def log_request_response(response: Response) -> Response:
            """Log the request/response after processing."""
            # Check if this path was excluded
            if getattr(request, "observability_excluded", False):
                return response

            # The 500 for an unhandled exception passes through here too; it
            # was already logged as a failure.
            if getattr(request, "observability_error_logged", False):
                return response

            # Check if we have request data (might not if before_request wasn't called)
            if not hasattr(request, "observability_start_time"):
                return response

            # Check if this is a streaming response - if so, handle it separately
            if hasattr(response, "response") and isinstance(response.response, IterableBody):
                # hands off the stream
                duration_ms = (time.time() - request.observability_start_time) * 1000

                # Get stored correlation ID from before_request
                correlation_id = getattr(request, "observability_correlation_id", None)
                request_data = getattr(request, "observability_request_data", {})

                # Re-extract user_id (might have been set during request processing)
                user_id = await self.request_adapter.extract_user_id(request)

                # Log with minimal response data (no body for streaming)
                self.logger.log_success(
                    request_data=request_data,
                    response_data={
                        "status_code": response.status_code
                        if hasattr(response, "status_code")
                        else 200,
                        "headers": dict(response.headers) if hasattr(response, "headers") else {},
                        "body": None,  # Don't try to extract body for streaming
                    },
                    duration_ms=duration_ms,
                    correlation_id=correlation_id,
                    user_id=user_id,
                )

                # Clear cache and return immediately
                self.request_adapter.clear_cache(request)
                return response

            # Non-streaming response handling
            # Calculate duration
            duration_ms = (time.time() - request.observability_start_time) * 1000

            # Get stored data
            correlation_id = getattr(request, "observability_correlation_id", None)
            request_data = getattr(request, "observability_request_data", {})

            # Re-extract user_id (might have been set during request processing)
            user_id = await self.request_adapter.extract_user_id(request)

            # Extract and prepare response data
            raw_response_data = await self.response_adapter.extract_all(response)
            response_data = self.logger.prepare_response_data(raw_response_data)

            # Log successful request
            self.logger.log_success(
                request_data=request_data,
                response_data=response_data,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                user_id=user_id,
            )

            # Clear cache
            self.request_adapter.clear_cache(request)

            return response

        async def log_request_exception(
            sender: Quart, exception: Exception, **extra: object
        ) -> None:
            """Log an unhandled exception with request context.

            A signal receiver rather than an error handler, so Quart still
            builds the response: HTTP errors (404, abort(403)) keep their
            status and are logged by after_request like any response, and
            the app's own error handlers still run.
            """
            # Check if this path was excluded
            if getattr(request, "observability_excluded", False):
                return

            # Check if we have request data
            if not hasattr(request, "observability_start_time"):
                return

            duration_ms = (time.time() - request.observability_start_time) * 1000
            correlation_id = getattr(request, "observability_correlation_id", None)
            request_data = getattr(request, "observability_request_data", {})

            # Get user_id if available
            user_id = await self.request_adapter.extract_user_id(request)

            # Log the error
            self.logger.log_error(
                request_data=request_data,
                error=exception,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                user_id=user_id,
            )
            request.observability_error_logged = True

            # Clear cache
            self.request_adapter.clear_cache(request)

        # The signal is process-wide: sender= limits it to this app's
        # requests. weak=False because nothing else references the closure.
        got_request_exception.connect(log_request_exception, sender=self.app, weak=False)

    async def process_request(self, request: Request, call_next) -> Response:
        """
        Process a request through the middleware pipeline.

        Note: This method is here to satisfy the BaseMiddleware interface,
        but Quart uses hooks instead of a middleware chain pattern.
        The actual processing happens in the registered hooks.

        Args:
            request: The incoming Quart request
            call_next: Not used in Quart implementation

        Returns:
            The Quart response
        """
        # This method is not directly used in Quart
        # The hooks handle the middleware functionality
        raise NotImplementedError(
            "Quart uses hooks instead of middleware chain. "
            "Use the QuartMiddleware constructor to register hooks."
        )


async def _record_span_correlation_id(sender: Quart, **extra: object) -> None:
    set_span_correlation_id(get_correlation_id())


def create_middleware(app: Quart, config: ObservabilityConfig) -> Quart:
    """
    Create and configure the complete Quart middleware stack.

    This function sets up correlation ID middleware and logging hooks, and,
    with ``config.tracing.instrument_server``, an OpenTelemetry server span
    per request (requires the ``otel`` extra).

    Args:
        app: Quart application
        config: Observability configuration

    Returns:
        The configured Quart application

    Example:
        ```python
        from quart import Quart
        from auditry.quart import create_middleware
        from auditry import ObservabilityConfig

        app = Quart(__name__)

        # Apply observability middleware
        app = create_middleware(
            app,
            config=ObservabilityConfig(service_name="my-service")
        )
        ```
    """
    _set_config_service(config.service_name)
    set_span_scrub_policy(SpanScrubPolicy.from_config(config))
    _set_correlation_header(config.correlation_id_header)

    # Create and register the middleware
    QuartMiddleware(app, config)

    if config.tracing.instrument_server:
        require_otel("tracing.instrument_server", "opentelemetry.instrumentation.asgi")
        # Imported here: .tracing needs the otel extra, which require_otel checked.
        from .tracing import instrument_quart  # noqa: PLC0415

        # After QuartMiddleware's CorrelationIdMiddleware wrap, so the span
        # is the outermost layer and active in every hook.
        instrument_quart(app, config)

    return app
