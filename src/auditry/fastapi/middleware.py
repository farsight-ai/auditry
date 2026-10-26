"""FastAPI middleware for observability."""

import logging
import time

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import Request
from starlette.types import Message, Receive

from ..core.base import declares_body
from ..core.logger import RequestResponseLogger
from ..correlation import get_correlation_id
from ..logging_config import _SERVER_ACCESS_LOGGER, _drop_own_handlers, _set_config_service
from ..models import ObservabilityConfig
from ..path_matcher import should_exclude_scope
from ..propagation import _set_correlation_header
from ..span_scrubbing import SpanScrubPolicy, set_span_scrub_policy
from ..tracing import require_otel, set_span_correlation_id
from .adapters import FastAPIRequestAdapter, FastAPIResponseAdapter


class FastAPIMiddleware:
    """ASGI middleware for request/response logging with streaming support."""

    def __init__(self, app, config: ObservabilityConfig):
        self.app = app
        self.config = config
        self.logger = RequestResponseLogger(config)
        self.request_adapter = FastAPIRequestAdapter()
        self.response_adapter = FastAPIResponseAdapter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        set_span_correlation_id(get_correlation_id())

        # Excluded paths skip logging. Both the decision and the logged path
        # come from the ASGI scope: request.url is rebuilt from the client's
        # Host header, which a client can craft.
        if should_exclude_scope(scope, self.config.excluded_paths):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        method = scope["method"]

        # With request body logging off the app reads the body straight from
        # the server: the middleware never sees it.
        raw_body = None
        app_receive = receive
        if self.config.log_request_body:
            raw_body, app_receive = await _buffer_request_body(receive)

        request = Request(scope, receive=app_receive)

        # Prepare logging context
        correlation_id = get_correlation_id()
        start_time = time.time()

        # Extract request data for logging
        raw_request_data = {
            "method": method,
            "path": path,
            "headers": dict(request.headers) if self.config.log_request_headers else None,
            "query_params": (dict(request.query_params) if self.config.log_query_params else None),
            "path_params": dict(getattr(request, "path_params", {})),
            "body": raw_body,
            "has_body": declares_body(request.headers),
            "user_id": await self.request_adapter.extract_user_id(request),
        }
        request_data = self.logger.prepare_request_data(raw_request_data, correlation_id)

        # Capture response info
        response_info = {"status": None, "headers": {}}
        response_body_chunks: list[bytes] = []
        response_has_body = False
        is_streaming = False

        async def capture_response(message):
            nonlocal is_streaming, response_has_body

            if message["type"] == "http.response.start":
                response_info["status"] = message["status"]

                # Capture headers
                for name, value in message.get("headers", []):
                    response_info["headers"][name.decode().lower()] = value.decode()

                # Detect streaming responses by content-type
                content_type = response_info["headers"].get("content-type", "")
                if "text/event-stream" in content_type:
                    is_streaming = True

            elif message["type"] == "http.response.body" and not is_streaming:
                # With response body logging off, only whether there was a body is kept.
                body = message.get("body", b"")
                if body:
                    response_has_body = True
                    if self.config.log_response_body:
                        response_body_chunks.append(body)

            await send(message)

        try:
            # Process the request
            await self.app(scope, app_receive, capture_response)

            # Log success
            duration_ms = (time.time() - start_time) * 1000
            user_id = await self.request_adapter.extract_user_id(request)

            response_body = b"".join(response_body_chunks) if response_body_chunks else None

            raw_response_data = {
                "status_code": response_info["status"],
                "headers": response_info["headers"] if self.config.log_response_headers else None,
                "body": response_body,
                "has_body": response_has_body,
            }
            response_data = self.logger.prepare_response_data(raw_response_data)

            self.logger.log_success(
                request_data=request_data,
                response_data=response_data,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                user_id=user_id,
            )

        except Exception as error:
            # Log error
            duration_ms = (time.time() - start_time) * 1000
            user_id = await self.request_adapter.extract_user_id(request)

            self.logger.log_error(
                request_data=request_data,
                error=error,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                user_id=user_id,
            )
            raise

        finally:
            # Clean up
            self.request_adapter.clear_cache(request)


async def _buffer_request_body(receive: Receive) -> tuple[bytes, Receive]:
    """Reads the whole request body, and returns it with a receive that hands
    it to the app once and then defers to the server's."""
    body_chunks = []
    message = await receive()

    while message["type"] == "http.request":
        body = message.get("body", b"")
        if body:
            body_chunks.append(body)
        if not message.get("more_body", False):
            break
        message = await receive()

    raw_body = b"".join(body_chunks)
    body_sent = False

    async def replay_body() -> Message:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        # Once the buffered body is replayed, defer to the REAL receive so
        # http.disconnect (and any further client messages) propagate to the
        # app. Returning a synthetic http.request here instead makes
        # StreamingResponse.listen_for_disconnect's `while True: await receive()`
        # a non-yielding hot loop (the coroutine never awaits real I/O), which
        # starves the single event loop and wedges the process.
        return await receive()

    return raw_body, replay_body


class _DropRecords(logging.Filter):
    """Rejects every record logged directly on the logger it is attached to, and holds the
    handlers and propagation it took from that logger so they can be given back."""

    def __init__(self, handlers: list[logging.Handler], propagate: bool) -> None:
        super().__init__()
        self.taken_handlers = handlers
        self.taken_propagate = propagate
        self.installed_handlers: list[logging.Handler] | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        return False

    def left_as_suppressed(self, access_logger: logging.Logger) -> bool:
        # The empty list is matched by identity: uvicorn's access_log=False and gunicorn's
        # worker leave the same empty, non-propagating state, each with a list of their own.
        return (
            access_logger.handlers is self.installed_handlers
            and not access_logger.handlers
            and not access_logger.propagate
        )


def _server_access_logger_and_drop() -> tuple[logging.Logger, _DropRecords | None]:
    access_logger = logging.getLogger(_SERVER_ACCESS_LOGGER)
    drop = next((f for f in access_logger.filters if isinstance(f, _DropRecords)), None)
    return access_logger, drop


def _suppress_server_access_log() -> None:
    """Turns off uvicorn's access line, which repeats the middleware's request line in plain
    text with the raw query string, outside redaction.

    Two layers. With no handler and no propagation, uvicorn does not build access lines at all:
    it checks for a handler as each connection opens, the state ``--no-access-log`` leaves. A
    server configured later, as ``uvicorn.run(app)`` is, re-installs that handler; the filter
    still drops the lines, because ``logging.config.dictConfig`` replaces a logger's handlers
    but keeps its filters (tests/test_server_logging.py fails if that ever changes).
    """
    access_logger, drop = _server_access_logger_and_drop()
    taken = (access_logger.handlers, access_logger.propagate)
    if drop is None:
        drop = _DropRecords(*taken)
        access_logger.addFilter(drop)
    elif not drop.left_as_suppressed(access_logger):
        # Reconfigured since the last suppression; what is there now is what to give back.
        drop.taken_handlers, drop.taken_propagate = taken
    _drop_own_handlers(access_logger)
    access_logger.propagate = False
    drop.installed_handlers = access_logger.handlers


def _restore_server_access_log() -> None:
    """Undoes ``_suppress_server_access_log``: the logger is process-wide, so the most recent
    ``create_middleware()`` decides. Handlers and propagation are given back only while the
    logger is as suppression left it; a server configured since has set its own."""
    access_logger, drop = _server_access_logger_and_drop()
    if drop is None:
        return
    access_logger.removeFilter(drop)
    if drop.left_as_suppressed(access_logger):
        access_logger.handlers = drop.taken_handlers
        access_logger.propagate = drop.taken_propagate


def create_middleware(app, config: ObservabilityConfig):
    """Create and attach observability middleware to FastAPI app.

    With ``config.tracing.instrument_server``, also starts an OpenTelemetry
    server span per request (requires the ``otel`` extra).
    """
    _set_config_service(config.service_name)
    set_span_scrub_policy(SpanScrubPolicy.from_config(config))
    _set_correlation_header(config.correlation_id_header)
    if config.suppress_server_access_log:
        _suppress_server_access_log()
    else:
        _restore_server_access_log()
    # Order matters: last added runs first
    app.add_middleware(FastAPIMiddleware, config=config)
    app.add_middleware(CorrelationIdMiddleware, header_name=config.correlation_id_header)
    if config.tracing.instrument_server:
        require_otel("tracing.instrument_server", "opentelemetry.instrumentation.fastapi")
        # Imported here: .tracing needs the otel extra, which require_otel checked.
        from .tracing import instrument_fastapi  # noqa: PLC0415

        # The instrumentor wraps the whole middleware stack when it is built,
        # so its span is active in both middlewares whatever the call order.
        instrument_fastapi(app, config)
    return app
