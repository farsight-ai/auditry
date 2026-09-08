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
  dimension names (`ForbiddenDimensionError`). Instrumentation never breaks
  the caller: on the emit path a violation drops the record and warns once
  per offending name; `strict=True` restores raising for tests/dev, and
  `default_dimensions` always raise at construction. Numbers and booleans
  coerce with `str()`; non-scalars are a `TypeError`. When
  `configure_logging()` has run, metric records ride the shared pipeline
  and carry the root schema plus `correlation_id`, so a metric ties back
  to the request that produced it (`sink=` is a raw-line test seam; raw
  stdout fallback when logging is unconfigured).
- **Strict mode** — one process-wide policy, resolved by `configure_logging()`:
  instrumentation failures (a bad metric dimension, a broken trace handler)
  raise in a *known* non-production environment (`local`, `dev`, `sandbox`,
  `test`, `staging`, …) and degrade to drop-and-warn everywhere else —
  production, unrecognized names, and no environment at all. Overrides:
  `configure_logging(strict=)`, `AUDITRY_STRICT`, `MetricsLogger(strict=)`;
  `is_strict()` exposes the result. Instrumentation never fails the unit of
  work it measures in production.
- `bound_correlation_id()` — context-manager form of `bind_correlation_id`
  that restores the previous context when the block ends; the recommended
  form for long-lived workers. `with_correlation` propagates an ID already
  bound in the context instead of starting a new trace. `outbound_headers()`
  defaults to the configured `correlation_id_header`, seeded by
  `create_middleware` (`X-Request-ID` remains the fallback).
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
- **Error logs** (auditry's own records: middleware lines and `get_logger()`
  users) no longer serialize tracebacks or `str(exception)` by default; they
  carry `error_type` (exception class name) + correlation ID. Exception
  messages and stack traces frequently interpolate user-supplied content; opt
  back in per service via `log_exception_messages` or the trace handler.
  `exception_type` is **removed** — it duplicated `error_type`; move
  dashboards keyed on it to `error_type`. Foreign stdlib records
  (`logging.getLogger(...)` in application or vendor code) share the root
  schema but **keep their tracebacks**.
- **Trace handler contract:** `set_trace_handler` handlers receive
  `(error_type, exc_info, event_dict)` — the live exception tuple, so error
  trackers can capture the real exception object; render text yourself if you
  need text. `AUDITRY_FULL_TRACEBACKS` is resolved once at `configure_logging()`
  and inlines the raw (JSON-escaped) traceback, not a `" | "`-flattened one.
- **One service identity:** `create_middleware` seeds the process-wide
  `service` from `ObservabilityConfig.service_name`; it takes precedence over
  `configure_logging(service=)` / `SERVICE_NAME`, which remain the fallback for
  processes without middleware (workers, scripts).
- **Query parameters now pass through redaction** — a token in a query string
  no longer bypasses the field-name redaction list.
- Expanded the default redaction list (`jwt`, `bearer`, `otp`, `access_key`,
  `private_key`, `csrf`, `x-amz-security-token`, …).
- Stdlib logging is explicitly bound to stdout.

### Fixed

- The correlation-ID response header is no longer emitted twice. Both the
  FastAPI and Quart middlewares were adding it manually on top of
  `CorrelationIdMiddleware`, which already appends it to every response;
  the manual additions are removed and asgi-correlation-id owns the header.

### Deprecated

- Implicit `log_request_body`/`log_response_body` defaults: both currently
  default to True but will default to **False** in a future release —
  request/response bodies are user-supplied content that field-name
  redaction cannot reliably scrub. Set the flags explicitly; a
  `DeprecationWarning` fires when the implicit default is used.
