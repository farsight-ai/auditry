"""Path matching utilities for middleware exclusions."""

import fnmatch
from typing import Optional, Union


def should_exclude_path(
    path: str, method: str, excluded_paths: Optional[Union[list[str], dict[str, list[str]]]]
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
