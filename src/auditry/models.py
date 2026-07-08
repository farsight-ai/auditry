import warnings
from typing import Optional, Dict, List, Union
from pydantic import BaseModel, Field, field_validator

# Health/liveness probes are excluded from request/response logging by
# default (they still get the correlation-ID header). A load balancer pings
# every task every 15-30s; unsuppressed, that noise inflates log cost and
# buries real errors.
DEFAULT_EXCLUDED_PATHS: List[str] = [
    "/health",
    "/healthz",
    "/livez",
    "/live",
    "/ready",
    "/readyz",
    "/api/health",
]


class BusinessEventConfig(BaseModel):
    """
    Configuration for a business event to track.

    Defines what event type to tag and what fields to extract for analytics.
    """

    event_type: str = Field(
        description="Business event type (e.g., 'folder.created', 'file.uploaded')"
    )
    extract_from_request: Optional[List[str]] = Field(
        default=None,
        description="Field names to extract from request body for business context"
    )
    extract_from_response: Optional[List[str]] = Field(
        default=None,
        description="Field names to extract from response body for business context"
    )
    extract_from_path: Optional[List[str]] = Field(
        default=None,
        description="Path parameter names to extract (e.g., ['folder_id'] for /folders/{folder_id})"
    )


class ObservabilityConfig(BaseModel):
    """
    Configuration options for observability logging behavior.

    Allows services to customize logging behavior including payload
    size limits, redaction patterns, and verbosity.
    """

    service_name: str = Field(
        min_length=1,
        description="Name of the service for logging context (e.g., 'vault-api', 'auth-service')"
    )
    correlation_id_header: str = Field(
        default="X-Request-ID",
        description="HTTP header name for correlation/request ID (default: X-Request-ID)",
    )

    @field_validator("service_name")
    @classmethod
    def validate_service_name(cls, v: str) -> str:
        """Validate service name is not empty or whitespace."""
        if not v or not v.strip():
            raise ValueError("service_name cannot be empty or whitespace")
        return v.strip()
    business_events: Optional[Dict[str, BusinessEventConfig]] = Field(
        default=None,
        description="Map of endpoint patterns to business event configurations for analytics tracking"
    )
    payload_size_limit: int = Field(
        default=10_240,
        description="Maximum size in bytes for logged request/response bodies (default 10KB)",
    )
    additional_redaction_patterns: Optional[list[str]] = Field(
        default=None,
        description="Additional field name patterns to redact beyond defaults",
    )
    log_request_headers: bool = Field(
        default=True, description="Whether to log request headers (redacted)"
    )
    log_response_headers: bool = Field(
        default=False, description="Whether to log response headers (redacted)"
    )
    log_query_params: bool = Field(default=True, description="Whether to log query parameters")
    log_request_body: bool = Field(
        default=True,
        description=(
            "Whether to log request bodies. NOTE: request bodies are "
            "user-supplied content that field-name redaction cannot reliably "
            "scrub (free text, documents, prompts). The default will change to "
            "False in a future release; services handling sensitive content "
            "should set False now (or scope body logging to safe endpoints)."
        )
    )
    log_response_body: bool = Field(
        default=True,
        description=(
            "Whether to log response bodies. NOTE: response bodies can carry "
            "sensitive generated/user content. The default will change to "
            "False in a future release; services handling sensitive content "
            "should set False now."
        )
    )
    log_exception_messages: bool = Field(
        default=False,
        description=(
            "Whether to include str(exception) in error logs. Off by default: "
            "exception messages frequently interpolate user content. The error "
            "TYPE and correlation ID are always logged; full tracebacks route "
            "via auditry.set_trace_handler to a gated destination."
        )
    )
    include_default_excluded_paths: bool = Field(
        default=True,
        description=(
            "Merge DEFAULT_EXCLUDED_PATHS (health/liveness probes) into "
            "excluded_paths (health-check log-spam suppression). Set False to "
            "opt out and manage exclusions entirely yourself."
        )
    )
    excluded_paths: Optional[Union[List[str], Dict[str, List[str]]]] = Field(
        default=None,
        description=(
            "Paths to exclude from observability middleware. "
            "Can be a list of path patterns (e.g., ['/health', '/metrics', '/stream*']) "
            "or a dict mapping HTTP methods to paths (e.g., {'GET': ['/health'], 'POST': ['/stream*']}). "
            "Supports wildcards (*) for pattern matching. Health/liveness probe "
            "paths are merged in by default (see include_default_excluded_paths)."
        )
    )

    def model_post_init(self, __context) -> None:
        # Deprecation notice: body logging will default to OFF in a future
        # release. Warn only when the service relies on the implicit default —
        # an explicit True is a deliberate, documented choice.
        implicit = [
            f
            for f in ("log_request_body", "log_response_body")
            if f not in self.model_fields_set and getattr(self, f)
        ]
        if implicit:
            warnings.warn(
                f"auditry: {' and '.join(implicit)} default to True today but "
                "will default to False in a future release (request/response "
                "bodies are user-supplied content that field-name redaction "
                "cannot reliably scrub). Set the flag(s) explicitly — False "
                "for services handling sensitive content.",
                DeprecationWarning,
                stacklevel=3,
            )
        # Merge the default health-probe exclusions.
        if self.include_default_excluded_paths:
            if self.excluded_paths is None:
                self.excluded_paths = list(DEFAULT_EXCLUDED_PATHS)
            elif isinstance(self.excluded_paths, list):
                merged = list(self.excluded_paths)
                merged += [p for p in DEFAULT_EXCLUDED_PATHS if p not in merged]
                self.excluded_paths = merged
            elif isinstance(self.excluded_paths, dict):
                get_paths = list(self.excluded_paths.get("GET", []))
                get_paths += [p for p in DEFAULT_EXCLUDED_PATHS if p not in get_paths]
                self.excluded_paths = {**self.excluded_paths, "GET": get_paths}
