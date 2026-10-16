"""ASGI tracing middleware shared by the FastAPI and Quart integrations."""

from typing import Any, Optional, Union

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import TracerProvider

from ..path_matcher import should_exclude_path

__all__ = ["TracingMiddleware"]


def _method_only_span_details(scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # The raw path can carry identifiers, so spans start named by method
    # alone; auditry.otel_context.annotate_server_span renames them to the
    # route template once the framework has routed the request.
    return scope.get("method") or scope.get("type", "HTTP"), {}


class TracingMiddleware:
    """
    Starts a server span per request through OpenTelemetry's ASGI middleware,
    except for ``excluded_paths``, which pass straight through untraced.

    Must be the outermost middleware so the span is active while auditry
    writes its request log line. Header capture is never configured, and the
    per-message ``receive``/``send`` child spans are disabled.
    """

    def __init__(
        self,
        app: Any,
        excluded_paths: Optional[Union[list[str], dict[str, list[str]]]] = None,
        tracer_provider: Optional[TracerProvider] = None,
    ) -> None:
        self.app = app
        self.excluded_paths = excluded_paths
        self.traced_app = OpenTelemetryMiddleware(
            app,
            default_span_details=_method_only_span_details,
            tracer_provider=tracer_provider,
            exclude_spans=["receive", "send"],
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and should_exclude_path(
            scope.get("path", ""), scope.get("method", ""), self.excluded_paths
        ):
            await self.app(scope, receive, send)
            return
        await self.traced_app(scope, receive, send)
