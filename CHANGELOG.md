# Changelog

## 0.6.3 (2026-10-26)

Output changes for Quart streamed responses: their lines lose `"body": null`,
and their response headers now follow `log_response_headers` (off by default)
and redaction. Other lines are unchanged, except that a request body sent
without `Content-Length` or `Transfer-Encoding` (possible only over HTTP/2) is
no longer marked `[BODY_LOGGING_DISABLED]`.

### Changed

- **A field whose logging is off is never extracted.** With
  `log_request_body=False` the FastAPI middleware no longer buffers the request
  body before the app runs; the app reads it from the server as it arrives.
  With `log_response_body=False` it keeps none of the response chunks it passes
  on. Quart's hooks no longer call `get_data()` on the request or response.
  `log_request_headers`, `log_response_headers` and `log_query_params` set to
  `False` likewise keep those fields from reaching the logger. Each used to be
  extracted in full and handed to the logger, which then left it out of the
  line.
- A request's `[BODY_LOGGING_DISABLED]` marks a body its headers declare (a
  `Content-Length` above 0, or a `Transfer-Encoding`), not one that was read.
  Under HTTP/1.1 a request body always has one of the two.
- Quart: a streamed response's line has no `body: null`.

### Fixed

- Quart: a streamed response's line logged every response header raw, with
  `Set-Cookie` unredacted, whatever `log_response_headers` said. It now goes
  through the same header flag and redaction as any other response.

### Added

- `BaseRequestAdapter.extract_all()` takes keyword-only `include_headers`,
  `include_query_params` and `include_body`, and
  `BaseResponseAdapter.extract_all()` takes `include_headers` and
  `include_body` (all default `True`). A field left out is None, and a body left
  out is never read. Both results carry `has_body`: whether the headers declare
  a body.
- `auditry.core.base.declares_body(headers)`.

## 0.6.2 (2026-10-23)

Default output changes for FastAPI services under uvicorn; check any query,
dashboard or alarm that reads uvicorn's access lines before upgrading. uvicorn's
`Exception in ASGI application` line is now JSON at level `error`, next to the
middleware's own error line for the same request, so a `$.level = "error"`
count (such as an error-count alarm) sees each unhandled exception twice.

### Changed

- **uvicorn's access log is off by default.** `create_middleware()` (FastAPI)
  leaves uvicorn's access logger with no handler, as `--no-access-log` does.
  The middleware already logs every request, with query parameters redacted;
  uvicorn's access line repeated it in plain text with the raw query string.
  `ObservabilityConfig(suppress_server_access_log=False)` keeps it. The logger
  is process-wide; the last `create_middleware()` call decides.
- **uvicorn's own lines are JSON.** `configure_logging()` drops the plain-text
  handlers uvicorn puts on `uvicorn` and `uvicorn.error`, so its startup,
  shutdown and server-error lines render through the root formatter like any
  other foreign record.
- **The suppression survives `uvicorn.run(app)`.** A server started in code
  configures logging after the app is built and, with uvicorn's default
  `log_config`, re-installs the access log's handler. A filter that drops every
  record stays on uvicorn's access logger through that, so its lines stay off.
  uvicorn's other lines go back to plain text there; `log_config=None` keeps
  them JSON.

### Added

- `ObservabilityConfig.suppress_server_access_log` (default `True`).

## 0.6.1 (2026-10-23)

### Added

- `MetricsLogger.dependency_call(..., ignore=(ExcType, ...))`: exception types
  that are the caller's own pre-flight failures (request validation before any
  network call) propagate untouched and record neither `Success` nor `Error`,
  so a bad request never counts against the dependency's availability.
- `conformance/`: auditry's log lines, metric records, redaction and strict
  mode recorded as data (`contract.json`, `vectors.json`), for ports to other
  languages to test against. `tests/test_conformance.py` fails when the code
  and the vectors disagree. Not in the wheel; in the sdist, for the tests.

### Documentation

- `dependency_call`: put timeouts inside the span (`asyncio.timeout()` or
  `wait_for` within the `with`) so they record as `ErrorType: TimeoutError`.
  A bare `asyncio.CancelledError` — client disconnect, lost race, TaskGroup
  sibling, shutdown, or a timeout placed outside the span — is re-raised
  unrecorded, as before: its cause is unknown to the span and it says nothing
  about the dependency.

## 0.6.0 (2026-10-19)

Optional OpenTelemetry tracing: trace IDs on log lines, W3C trace context
across HTTP and SQS/SNS hops, a span exporter that scrubs sensitive data, and
opt-in server spans. Everything new needs the `otel` extra, except trace IDs
on log lines and trace context propagation, which turn on whenever the
OpenTelemetry API is importable, as it is wherever Langfuse is installed.
`pip install auditry` still pulls in no OpenTelemetry.

### Breaking

- **Python 3.11 or later.** OpenTelemetry 1.42.0 and later require Python
  3.10+, and every internal consumer runs 3.11 or 3.12. Services on 3.9 or 3.10
  stay on 0.5.x.
- **`trace_id` and `span_id` are reserved in `MetricsLogger`.** A metric or
  dimension with either name is dropped with a warning (`MetricRecordError` in
  strict mode), because inside a span the log pipeline writes those fields.

### Added

- The `otel` extra: `pip install 'auditry[otel]'` (also in `all`).
- `trace_id` / `span_id` on every log line written inside a valid span context,
  including foreign stdlib records and EMF metric records. Optional fields:
  absent outside a span and without the extra.
- W3C trace context propagation. `outbound_headers()` adds `traceparent` /
  `tracestate` inside a span (a trace header already in `extra` is kept;
  `trace_context=False` leaves them out, for a third party);
  `sqs_message_attributes()` adds them as message attributes within the SQS
  limit of 10, warning once (`MessageAttributeLimitError` in strict mode) when
  they do not fit. W3C Trace Context only, whatever `OTEL_PROPAGATORS` says;
  `baggage` is never sent.
- `bound_message_context(message)`: binds a received message's correlation ID
  and attaches its trace context for a block, then restores both.
  `extract_trace_context(message)` returns the context without attaching it.
  `bind_from_sqs_message()` now also attaches the trace context (sticky until
  the next call, so a message without one does not inherit the previous one's).
- `correlation_id` recorded as an attribute on the current server span, from
  auditry's own server instrumentation or the service's.
- `auditry.otel.ScrubbingSpanExporter`, which removes credentials, PII,
  exception messages, stack traces and the customer content its rules recognize
  from every span before handing it to the wrapped exporter, and drops a span
  rather than export it unscrubbed. Client span URL paths are exported unchanged
  apart from email addresses; see the README's Limitations. Langfuse SDK spans,
  which reach it through the global tracer provider, keep only operational
  `langfuse.*` attributes. `ScrubbingOTLPSpanExporter` and
  `ScrubbingConsoleSpanExporter` are registered as the
  `opentelemetry_traces_exporter` entry points `auditry_otlp` and
  `auditry_console`, for zero-code setup.
- `TracingConfig` (`ObservabilityConfig.tracing`): `instrument_server`,
  `query_param_allowlist`, `db_statement` and `keep_client_address`.
- Opt-in server spans (`tracing.instrument_server=True`) for FastAPI
  (`FastAPIInstrumentor`) and Quart (OpenTelemetry's ASGI middleware, spans
  renamed to the route rule), skipping exactly the requests request logging
  skips: `excluded_paths` matched against the path inside the app and the
  method, method-specific patterns included; `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`
  and `OTEL_PYTHON_EXCLUDED_URLS` are ignored, since they match a URL built
  from the client's `Host` header. An app that is already instrumented is left
  alone, wherever its instrumentation sits among the ASGI wrappers.
- CI runs the test suite on Python 3.11 and 3.12. The check names become
  `pytest (3.11)` and `pytest (3.12)`.

### Fixed

- **FastAPI: excluded paths and the logged path come from the ASGI path.**
  Request logging read `request.url.path`, which Starlette rebuilds from the
  client's `Host` header, so a `Host` value containing `/` could skip logging
  for an excluded pattern or change the path a line records. Exclusions now
  match the path inside the app (any ASGI `root_path` removed, as Quart and
  Starlette's routing do) and the line records the ASGI path.
- **Quart: HTTP errors keep their status.** auditry's Quart error handler
  re-raised every exception, so a 404, an `abort(403)` or any other HTTP error
  left the app without a response and the server sent a 500, and the app's own
  `errorhandler(500)` never ran. Unhandled exceptions are now logged from
  Quart's `got_request_exception` signal instead, and Quart builds the response
  as it would without auditry. What changes in the logs: an HTTP error is a
  `Request completed` line with its real status rather than two
  `Request failed` lines, and an unhandled exception is one `Request failed`
  line rather than two. An exception the app's own handler turns into a 5XX
  response, such as a catch-all `errorhandler(Exception)`, is also one
  `Request failed` line, as on FastAPI; one turned into another status is a
  `Request completed` line. Log-derived error counts drop; check alarms on them.
- `auditry.__version__` reported 0.4.0 in the 0.5.0 release; it now matches the
  package version.

## 0.5.0 (2026-09-30)

The 0.4.0 wheel on PyPI was built before the full 0.4.0 changes merged, so two
0.4.0 items reach PyPI for the first time in this release (see *Included from
0.4.0*). Upgrading from the published 0.4.0: health-probe requests stop being
logged, and a `DeprecationWarning` can fail test suites that treat warnings as
errors. Upgrading from 0.3.x: follow the 0.4.0 steps below. Nothing extra is
needed.

### Included from 0.4.0

Both items are described under 0.4.0 but are missing from the 0.4.0 wheel on PyPI.

- Health/liveness probe paths (`/health`, `/healthz`, `/livez`, `/live`,
  `/ready`, `/readyz`, `/api/health`) are excluded from request/response
  logging by default. Log-derived request counts drop; check low-log-volume
  alarms before rollout. Opt out with `include_default_excluded_paths=False`.
- A `DeprecationWarning` fires on `ObservabilityConfig` construction when
  `log_request_body` / `log_response_body` are left implicit. Test suites
  running `-W error` fail until both flags are set explicitly.

### Added

- `MetricsLogger.emit(dimension_sets=...)` records a metric under exactly the
  listed dimension sets, so two independent breakdowns of one event (say
  `[Service, Route]` and `[Service, Tenant]`) no longer create their cross
  product. Still one log line per call. A set naming a missing dimension, a
  duplicate set, a set of more than 30 keys (the EMF per-set limit), an empty
  list, or combining it with `rollup_dimension_sets` is dropped with a warning,
  or raises in strict mode.

### Fixed

- README: the `rollup_dimension_sets` example now names `Service` in its rollup,
  so the set it records matches its comment. Rollup sets are recorded as given.

## 0.4.0 (2026-09-23)

The wheel published to PyPI as 0.4.0 does not include the default health-probe
path exclusion or the body-logging `DeprecationWarning` listed below. Both
ship in 0.5.0.

Hardening release: safer defaults for sensitive content, first-class
correlation propagation outside ASGI, and an EMF metrics helper.

### Upgrading from 0.3.x

Nothing breaks at import or construction time: no exports were removed, no
signature changed incompatibly, and every new `ObservabilityConfig` field has a
default. What changes is **what the log lines look like**, so the breakage
surfaces in the tooling that reads them, not in the service.

Breaking for consumers of the logs:

1. **`event` → `message`.** Any saved Logs Insights query, metric filter, or
   dashboard referencing `event` stops matching — and metric-filter alarms fail
   *silently* (a filter that matches nothing looks like a healthy service).
   Audit metric filters before upgrading a service with alarms on log patterns.
2. **Error lines carry `error_type` + correlation ID only.** No traceback, no
   `exception_message`, and `exception_type` is removed (it duplicated
   `error_type`; move dashboards keyed on it). The human-readable message no
   longer embeds the class name. Applies to auditry's own records only —
   stdlib loggers in application or vendor code keep their tracebacks. A
   registered trace handler receives the live `exc_info` tuple, not text.
3. **Health-probe requests are no longer logged.** Log-derived request counts
   drop, sometimes sharply; check any low-log-volume alarm before rollout.
4. **Query parameters are now redacted**, along with 14 additional field
   patterns (`signature`, `credential`, …) that now read `[REDACTED]`.
5. **Timestamps are explicitly UTC** (previously the container's local time)
   and stdlib logging is explicitly on **stdout** (`basicConfig` defaulted to
   stderr). No-ops under the awslogs driver; they matter if anything downstream
   separates the streams or parses local timestamps.
6. **A `DeprecationWarning` fires on `ObservabilityConfig` construction** when
   `log_request_body` / `log_response_body` are left implicit. A test suite
   running `-W error` / `filterwarnings = ["error"]` fails until they are set.

Steps:

1. Pass `service` / `version` / `environment` to `configure_logging()` (or set
   `SERVICE_NAME` / `SERVICE_VERSION` / `ENVIRONMENT`) — in worker entrypoints
   too. `environment` also decides strict mode (see *Added*).
2. Set `log_request_body` / `log_response_body` explicitly; services handling
   customer content choose `False`.
3. Update saved Logs Insights queries and metric filters: `event` → `message`,
   `exception_type` → `error_type`.
4. In workers, bind a correlation ID before the first log line and add
   `outbound_headers()` to outbound calls.
5. Decide on tracebacks: register `set_trace_handler(...)` to route them to a
   gated destination, or accept `error_type` only.

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
  per offending name; strict mode (below) restores raising where it is
  cheap, and `default_dimensions` always raise at construction. Numbers and booleans
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
