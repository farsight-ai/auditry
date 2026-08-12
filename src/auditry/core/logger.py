"""
Core request/response logging logic.

This module contains the framework-agnostic logging implementation that processes
request and response data captured by the framework adapters.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ..models import ObservabilityConfig, BusinessEventConfig
from ..redaction import redact_data, redact_headers


class RequestResponseLogger:
    """
    Framework-agnostic logger for HTTP requests and responses.

    This class handles the actual logging logic including:
    - Parsing and redacting request/response data
    - Extracting business events
    - Formatting log messages
    - Managing structured logging context
    """

    def __init__(self, config: ObservabilityConfig):
        """
        Initialize the logger with configuration.

        Args:
            config: Observability configuration containing service name,
                   redaction patterns, business events, etc.
        """
        self.config = config
        self.service_name = config.service_name
        self.payload_size_limit = config.payload_size_limit
        self.additional_redaction_patterns = config.additional_redaction_patterns or []
        self.business_events = config.business_events or {}
        self.logger = structlog.get_logger(__name__)

    def prepare_request_data(
        self,
        raw_data: Dict[str, Any],
        correlation_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Prepare request data for logging.

        This method:
        - Adds correlation ID
        - Redacts headers if configured
        - Parses and redacts the body
        - Filters data based on configuration

        Args:
            raw_data: Raw request data from adapter
            correlation_id: Optional correlation ID for the request

        Returns:
            Prepared request data ready for logging
        """
        prepared = {
            "method": raw_data.get("method", "UNKNOWN"),
            "path": raw_data.get("path", "/"),
            "correlation_id": correlation_id,
            "user_id": raw_data.get("user_id"),
        }

        # Add headers if configured
        if self.config.log_request_headers and raw_data.get("headers"):
            prepared["headers"] = redact_headers(raw_data["headers"])

        # Add query params if configured (redacted — a token in a query
        # string must not bypass the redaction list)
        if self.config.log_query_params and raw_data.get("query_params"):
            prepared["query_params"] = redact_data(
                raw_data["query_params"], self.additional_redaction_patterns
            )

        # Add path params (always included if present)
        if raw_data.get("path_params"):
            prepared["path_params"] = raw_data["path_params"]

        # Parse and redact body (only if configured to log request body)
        if self.config.log_request_body and raw_data.get("body"):
            prepared["body"] = self._parse_and_redact_body(raw_data["body"])
        elif raw_data.get("body") and not self.config.log_request_body:
            prepared["body"] = "[BODY_LOGGING_DISABLED]"

        return prepared

    def prepare_response_data(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare response data for logging.

        This method:
        - Redacts headers if configured
        - Parses and redacts the body
        - Filters data based on configuration

        Args:
            raw_data: Raw response data from adapter

        Returns:
            Prepared response data ready for logging
        """
        prepared = {
            "status_code": raw_data.get("status_code", 0),
        }

        # Add headers if configured
        if self.config.log_response_headers and raw_data.get("headers"):
            prepared["headers"] = redact_headers(raw_data["headers"])

        # Parse and redact body (only if configured to log response body)
        if self.config.log_response_body and raw_data.get("body"):
            prepared["body"] = self._parse_and_redact_body(raw_data["body"])
        elif raw_data.get("body") and not self.config.log_response_body:
            prepared["body"] = "[BODY_LOGGING_DISABLED]"

        return prepared

    def log_success(
        self,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        duration_ms: float,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Log a successful request/response cycle.

        Args:
            request_data: Prepared request data
            response_data: Prepared response data
            duration_ms: Request duration in milliseconds
            correlation_id: Optional correlation ID
            user_id: Optional user ID
        """
        self._bind_context(correlation_id, user_id)

        # Extract business event if applicable
        event_type, business_context = self._extract_business_event(
            request_data, response_data
        )

        # Build log entry
        log_entry = {
            "service": self.service_name,
            "request": request_data,
            "response": response_data,
            "execution_duration_ms": duration_ms,
        }

        if event_type:
            log_entry["event_type"] = event_type
            log_entry["business_context"] = business_context

        # Log the entry
        self.logger.info(
            f"Request completed: {request_data['method']} {request_data['path']} - "
            f"Status: {response_data['status_code']} - Duration: {duration_ms:.2f}ms",
            **log_entry
        )

    def log_error(
        self,
        request_data: Dict[str, Any],
        error: Exception,
        duration_ms: float,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Log a failed request.

        Args:
            request_data: Prepared request data
            error: The exception that occurred
            duration_ms: Request duration in milliseconds
            correlation_id: Optional correlation ID
            user_id: Optional user ID
        """
        self._bind_context(correlation_id, user_id)

        # Build log entry.
        # The error TYPE and correlation ID are safe to log freely. The
        # exception MESSAGE and traceback can interpolate sensitive user
        # content, so they are not serialized here — exc_info is passed
        # through so the configured processor extracts error_type and routes
        # the full trace to the gated handler, if any (see
        # auditry.logging_config.set_trace_handler).
        log_entry = {
            "service": self.service_name,
            "request": request_data,
            "execution_duration_ms": duration_ms,
            "error_type": type(error).__name__,
            # Kept for backward compatibility with existing dashboards:
            "exception_type": type(error).__name__,
        }
        if self.config.log_exception_messages:
            # Explicit opt-in only — see the note on the config field.
            log_entry["exception_message"] = str(error)

        # Log the error. exc_info is the explicit tuple for the *supplied*
        # exception, not exc_info=True: log_error may be called after the
        # caller's except block has ended (or for a different exception than
        # the currently-active one), and sys.exc_info() would then be empty
        # or wrong.
        self.logger.error(
            f"Request failed: {request_data['method']} {request_data['path']} - "
            f"Error: {type(error).__name__} - Duration: {duration_ms:.2f}ms",
            exc_info=(type(error), error, error.__traceback__),
            **log_entry
        )

    def _bind_context(self, correlation_id: Optional[str], user_id: Optional[str]) -> None:
        """Bind correlation ID and user ID to the structured logging context."""
        if correlation_id:
            structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        if user_id:
            structlog.contextvars.bind_contextvars(user_id=user_id)

    def _parse_and_redact_body(self, body: Optional[bytes]) -> Optional[Any]:
        """
        Parse body bytes and apply redaction.

        Args:
            body: Raw body bytes

        Returns:
            Parsed and redacted body, or None if empty
        """
        if not body:
            return None

        # Check size limit
        if len(body) > self.payload_size_limit:
            return {
                "_truncated": True,
                "_original_size": len(body),
                "_preview": body[:self.payload_size_limit].decode("utf-8", errors="replace"),
            }

        # Try to parse as JSON
        try:
            body_json = json.loads(body)
            return redact_data(body_json, self.additional_redaction_patterns)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Return as string if not JSON
            try:
                return body.decode("utf-8", errors="replace")
            except Exception:
                return "<binary data>"

    def _extract_business_event(
        self,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Extract business event information if the request matches a configured pattern.

        Args:
            request_data: Prepared request data
            response_data: Prepared response data

        Returns:
            Tuple of (event_type, business_context) or (None, None)
        """
        if not self.business_events:
            return None, None

        method = request_data.get("method", "")
        path = request_data.get("path", "")
        endpoint = f"{method} {path}"

        # Find matching business event configuration
        for pattern, event_config in self.business_events.items():
            if self._matches_pattern(endpoint, pattern):
                context = self._extract_business_context(
                    event_config,
                    request_data,
                    response_data
                )
                return event_config.event_type, context

        return None, None

    def _matches_pattern(self, endpoint: str, pattern: str) -> bool:
        """
        Check if an endpoint matches a pattern.

        Supports patterns like "POST /users" or "GET /users/{id}".

        Args:
            endpoint: The actual endpoint (e.g., "POST /users")
            pattern: The pattern to match against

        Returns:
            True if the endpoint matches the pattern
        """
        # Split method and path
        try:
            endpoint_method, endpoint_path = endpoint.split(" ", 1)
            pattern_method, pattern_path = pattern.split(" ", 1)
        except ValueError:
            return False

        # Methods must match exactly
        if endpoint_method != pattern_method:
            return False

        # Convert pattern to regex (replace {param} with regex)
        # {id} becomes ([^/]+) to match any non-slash characters
        pattern_regex = re.sub(r"\{[^}]+\}", r"([^/]+)", pattern_path)
        pattern_regex = f"^{pattern_regex}$"

        return bool(re.match(pattern_regex, endpoint_path))

    def _extract_business_context(
        self,
        event_config: BusinessEventConfig,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract business context fields from request and response data.

        Args:
            event_config: Business event configuration
            request_data: Prepared request data
            response_data: Prepared response data

        Returns:
            Dictionary of extracted business context fields
        """
        context = {}

        # Extract from request body
        if event_config.extract_from_request and request_data.get("body"):
            body = request_data["body"]
            if isinstance(body, dict):
                for field in event_config.extract_from_request:
                    if field in body:
                        context[field] = body[field]

        # Extract from response body
        if event_config.extract_from_response and response_data.get("body"):
            body = response_data["body"]
            if isinstance(body, dict):
                for field in event_config.extract_from_response:
                    if field in body:
                        context[field] = body[field]

        # Extract from path parameters
        if event_config.extract_from_path and request_data.get("path_params"):
            for param in event_config.extract_from_path:
                if param in request_data["path_params"]:
                    context[param] = request_data["path_params"][param]

        return context