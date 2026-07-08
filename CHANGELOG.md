# Changelog

## 0.4.0 (unreleased)

Hardening release: safer defaults for sensitive content, first-class
correlation propagation outside ASGI, and an EMF metrics helper.

### Added

- `configure_logging(service=..., version=..., environment=...)` (or
  `SERVICE_NAME`/`SERVICE_VERSION`/`ENVIRONMENT` env vars) stamps a consistent
  root schema on every log line.
- The correlation ID is attached to **every** log line automatically, not
  just middleware request/response entries.
- `auditry.propagation` — correlation-ID helpers beyond the request cycle:
  `bind_correlation_id` / `with_correlation` for queue workers,
  `outbound_headers` for HTTP calls, `sqs_message_attributes` /
  `bind_from_sqs_message` for SQS/SNS hops.
- `auditry.metrics.MetricsLogger` — dependency-free CloudWatch EMF emitter
  with `dependency_call()` timing, per-error-reason counts, zero-count
  support for no-data alarms, and validation that rejects PII/user-content
  dimension names (`ForbiddenDimensionError`).
- `set_trace_handler(...)` — route full exception tracebacks to a gated
  destination of your choice; `AUDITRY_FULL_TRACEBACKS=true` inlines them for
  local development.
- `ObservabilityConfig.log_exception_messages` (default **False**).
- Health/liveness probe paths (`/health`, `/healthz`, `/ready`, …) are
  excluded from request/response logging by default
  (`include_default_excluded_paths=False` to opt out).

### Changed

- **Log schema:** the message key is now `message` (was structlog's `event`),
  and `service`/`version`/`environment` appear on every line — update saved
  queries that referenced `event`.
- **Error logs** no longer serialize tracebacks or `str(exception)` by
  default; they carry `error_type` (exception class name) + correlation ID.
  Exception messages and stack traces frequently interpolate user-supplied
  content; opt back in per service via `log_exception_messages` or the trace
  handler.
- **Query parameters now pass through redaction** — a token in a query string
  no longer bypasses the field-name redaction list.
- Expanded the default redaction list (`jwt`, `bearer`, `otp`, `access_key`,
  `private_key`, `csrf`, `x-amz-security-token`, …).
- Stdlib logging is explicitly bound to stdout.

### Deprecated

- Implicit `log_request_body`/`log_response_body` defaults: both currently
  default to True but will default to **False** in a future release —
  request/response bodies are user-supplied content that field-name
  redaction cannot reliably scrub. Set the flags explicitly; a
  `DeprecationWarning` fires when the implicit default is used.
