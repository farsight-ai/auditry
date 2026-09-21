from typing import Any, Dict, List, Set


DEFAULT_REDACTION_PATTERNS: List[str] = [
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "ssn",
    "social_security_number",
    "credit_card",
    "creditcard",
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "jwt",
    "bearer",
    "otp",
    "one_time_password",
    "access_key",
    "private_key",
    "signature",
    "credential",
    "x-auth",
    "x-amz-security-token",
    "proxy-authorization",
    "csrf",
    "xsrf",
]

REDACTED_VALUE = "[REDACTED]"


class SensitiveDataRedactor:
    """
    Redacts sensitive data from dictionaries and JSON-serializable objects.

    Uses field name pattern matching to identify and replace sensitive
    values with a redaction marker. Supports nested dictionaries and lists.
    """

    def __init__(self, additional_patterns: List[str] = None):
        """
        Initialize redactor with field patterns to redact.

        Args:
            additional_patterns: Additional field name patterns beyond defaults
        """
        self.patterns: Set[str] = set(pattern.lower() for pattern in DEFAULT_REDACTION_PATTERNS)
        if additional_patterns:
            self.patterns.update(pattern.lower() for pattern in additional_patterns)

    def should_redact(self, field_name: str) -> bool:
        """
        Determine if a field should be redacted based on its name.

        Performs case-insensitive matching against known sensitive
        field patterns.

        Args:
            field_name: Name of the field to check

        Returns:
            True if field should be redacted, False otherwise
        """
        field_lower = field_name.lower()
        # Exact match
        if field_lower in self.patterns:
            return True
        # Partial match (e.g., "user_password" matches "password")
        return any(pattern in field_lower for pattern in self.patterns)

    def redact(self, data: Any) -> Any:
        """
        Recursively redact sensitive data from a data structure.

        Handles dictionaries, lists, and primitive types. Creates a new
        structure with sensitive values replaced, leaving original unchanged.

        Args:
            data: Data structure to redact (dict, list, or primitive)

        Returns:
            New data structure with sensitive values redacted
        """
        if isinstance(data, dict):
            return self._redact_dict(data)
        elif isinstance(data, list):
            return self._redact_list(data)
        else:
            # Primitive type, return as-is
            return data

    def _redact_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Redact sensitive fields in a dictionary.

        Creates new dictionary with sensitive field values replaced
        with redaction marker. Recursively processes nested structures.
        """
        redacted = {}
        for key, value in data.items():
            if self.should_redact(key):
                redacted[key] = REDACTED_VALUE
            elif isinstance(value, dict):
                redacted[key] = self._redact_dict(value)
            elif isinstance(value, list):
                redacted[key] = self._redact_list(value)
            else:
                redacted[key] = value
        return redacted

    def _redact_list(self, data: List[Any]) -> List[Any]:
        """
        Redact sensitive data in a list.

        Recursively processes each list element, handling nested
        dictionaries and lists.
        """
        return [self.redact(item) for item in data]


_default_redactor = SensitiveDataRedactor()


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Redact sensitive headers for logging.

    Specifically handles common authentication and API key headers
    that should never appear in logs.

    Args:
        headers: Dictionary of HTTP headers

    Returns:
        New dictionary with sensitive headers redacted
    """
    return _default_redactor.redact(headers)


def redact_data(data: Any, additional_patterns: List[str] = None) -> Any:
    """
    Convenience function to redact sensitive data.

    Args:
        data: Data structure to redact
        additional_patterns: Optional additional field patterns to redact

    Returns:
        Redacted copy of the data
    """
    if additional_patterns:
        # Only create new instance if custom patterns are needed
        redactor = SensitiveDataRedactor(additional_patterns)
        return redactor.redact(data)
    return _default_redactor.redact(data)