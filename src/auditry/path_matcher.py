"""Path matching utilities for middleware exclusions."""

import fnmatch
from collections.abc import Mapping
from typing import Any

# The ASGI scope types a request can be excluded from, and the method a
# websocket scope stands for: it has none of its own, and its handshake is a GET.
EXCLUDABLE_SCOPE_TYPES = ("http", "websocket")
WEBSOCKET_HANDSHAKE_METHOD = "GET"


def should_exclude_path(
    path: str, method: str, excluded_paths: list[str] | dict[str, list[str]] | None
) -> bool:
    """
    Check if a request path should be excluded from middleware processing.

    Args:
        path: The request path
        method: The HTTP method
        excluded_paths: Can be None, a list of patterns, or a dict with method-specific patterns

    Returns:
        True if path should be excluded

    Examples:
        >>> # Exclude all methods
        >>> should_exclude_path('/health', 'GET', ['/health', '/metrics'])
        True

        >>> # Method-specific exclusion
        >>> should_exclude_path('/stream', 'GET', {'GET': ['/stream*']})
        True

        >>> # Wildcard patterns
        >>> should_exclude_path('/api/v1/stream/events', 'POST', ['/api/*/stream/*'])
        True
    """
    if excluded_paths is None:
        return False

    # strip query params
    if "?" in path:
        path = path.split("?")[0]

    if isinstance(excluded_paths, list):
        return _match_path_patterns(path, excluded_paths)

    if isinstance(excluded_paths, dict):
        method_patterns = excluded_paths.get(method.upper(), [])
        if _match_path_patterns(path, method_patterns):
            return True

        # also check wildcard patterns
        all_patterns = excluded_paths.get("*", [])
        if _match_path_patterns(path, all_patterns):
            return True

    return False


def _match_path_patterns(path: str, patterns: list[str]) -> bool:
    """Check if path matches any pattern."""
    for pattern in patterns:
        if pattern == path:
            return True

        # wildcard matching
        if ("*" in pattern or "?" in pattern) and fnmatch.fnmatch(path, pattern):
            return True

        # prefix matching
        if pattern.endswith("/") and path.startswith(pattern):
            return True

    return False


def app_path(scope: Mapping[str, Any]) -> str:
    """The request's path inside the app: the ASGI path without the
    ``root_path`` the app is mounted under, as Quart's ``request.path`` and
    Starlette's routing see it. Never built from the client's Host header,
    which a client can craft."""
    # Starlette's rule (starlette._utils.get_route_path): strip root_path only
    # at a segment boundary, and the root itself is an empty path, which no
    # pattern matches, as Quart's placeholder for it matches none either.
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    return path[len(root_path) :] if path[len(root_path)] == "/" else path


def should_exclude_scope(
    scope: Mapping[str, Any], excluded_paths: list[str] | dict[str, list[str]] | None
) -> bool:
    """``should_exclude_path`` for an ASGI request, from its path inside the app
    and its method."""
    return scope["type"] in EXCLUDABLE_SCOPE_TYPES and should_exclude_path(
        app_path(scope), scope.get("method", WEBSOCKET_HANDSHAKE_METHOD), excluded_paths
    )
