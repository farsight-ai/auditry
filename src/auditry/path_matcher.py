"""Path matching utilities for middleware exclusions."""

import fnmatch
import re


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


def excluded_paths_to_url_patterns(
    excluded_paths: list[str] | dict[str, list[str]] | None,
) -> list[str]:
    """
    Translate ``excluded_paths`` into OpenTelemetry ``excluded_urls`` regexes,
    so FastAPI server spans skip the same paths request logging skips.

    OpenTelemetry matches each regex with ``re.search`` against
    ``scheme://host`` plus the path, with no query and no method. The dict
    form therefore contributes only its ``"*"`` key: method-specific
    exclusions cannot be expressed, and those requests still get spans.

    Raises ValueError for a pattern containing ``,``, because
    ``FastAPIInstrumentor`` takes ``excluded_urls`` as a comma-separated string.
    """
    if excluded_paths is None:
        return []
    patterns = excluded_paths if isinstance(excluded_paths, list) else excluded_paths.get("*", [])
    url_patterns = []
    for pattern in patterns:
        if "," in pattern:
            raise ValueError(
                f"excluded path {pattern!r} contains ',', which OpenTelemetry's "
                "comma-separated excluded_urls cannot express"
            )
        # The same three ways _match_path_patterns matches.
        alternatives = [re.escape(pattern) + "$"]
        if "*" in pattern or "?" in pattern:
            alternatives.append(fnmatch.translate(pattern))
        if pattern.endswith("/"):
            alternatives.append(re.escape(pattern))
        # The host comes from the client's Host header, and the lookahead pins
        # the match to the first "/" after the scheme. That is the start of the
        # path unless the Host value itself contains "/", so such a client can
        # line a multi-segment, wildcard or prefix pattern up with part of its
        # Host value and leave its own request untraced.
        url_patterns.append("^[^:/]+://[^/]*(?=/)(?:" + "|".join(alternatives) + ")")
    return url_patterns
