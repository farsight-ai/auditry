"""ASGI plumbing shared by the FastAPI and Quart server spans. Requires the
``otel`` extra."""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from ..path_matcher import should_exclude_scope

Scope = MutableMapping[str, Any]
ASGIApp = Callable[..., Awaitable[None]]


class TracedUnlessExcluded:
    """Sends a request to ``traced``, OpenTelemetry's middleware around
    ``app``, unless ``excluded_paths`` matches it, in which case it goes
    straight to ``app`` and gets no span.

    The decision uses the request's path inside the app and its method, as
    request logging does. OpenTelemetry's own ``excluded_urls`` would match a
    URL built from the client's Host header, which a client can craft to skip
    its span, and cannot see the method."""

    def __init__(
        self,
        app: ASGIApp,
        traced: ASGIApp,
        excluded_paths: list[str] | dict[str, list[str]] | None,
    ) -> None:
        self.app = app
        self.traced = traced
        self.excluded_paths = excluded_paths

    async def __call__(self, scope: Scope, receive: ASGIApp, send: ASGIApp) -> None:
        excluded = should_exclude_scope(scope, self.excluded_paths)
        await (self.app if excluded else self.traced)(scope, receive, send)
