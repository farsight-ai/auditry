"""ObservabilityConfig's per-field logging flags, for tests that turn one off."""

from auditry import ObservabilityConfig

ALL_FIELDS_LOGGED = {
    "log_request_headers": True,
    "log_response_headers": True,
    "log_query_params": True,
    "log_request_body": True,
    "log_response_body": True,
}

# Each flag, the side of the exchange it governs, and the raw field it leaves out.
FIELD_FLAGS = [
    ("log_request_headers", "request", "headers"),
    ("log_response_headers", "response", "headers"),
    ("log_query_params", "request", "query_params"),
    ("log_request_body", "request", "body"),
    ("log_response_body", "response", "body"),
]


def config_with_field_off(flag: str) -> ObservabilityConfig:
    """Every field logged except the one ``flag`` governs."""
    return ObservabilityConfig(service_name="svc", **{**ALL_FIELDS_LOGGED, flag: False})
