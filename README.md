# auditry

A clean, framework-agnostic observability middleware for FastAPI and Quart that provides comprehensive request/response logging, correlation ID tracking, business event extraction, and sensitive data redaction.

[![PyPI version](https://badge.fury.io/py/auditry.svg)](https://badge.fury.io/py/auditry)
[![Python Versions](https://img.shields.io/pypi/pyversions/auditry.svg)](https://pypi.org/project/auditry/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Installation

### For FastAPI

```bash
pip install auditry[fastapi]
```

### For Quart

```bash
pip install auditry[quart]
```

### For everything

```bash
pip install auditry[all]
```

## Quick Start

### FastAPI

```python
from fastapi import FastAPI
from auditry import configure_logging, ObservabilityConfig, get_logger
from auditry.fastapi import create_middleware

# Configure structured logging at startup.
# service/version/environment are stamped on every line — pass them here or set
# SERVICE_NAME / SERVICE_VERSION / ENVIRONMENT.
configure_logging(
    level="INFO",
    service="my-service",
    version="1.0.0",
    environment="prod",
)

app = FastAPI()

# Add observability middleware (single line!)
app = create_middleware(
    app,
    config=ObservabilityConfig(
        service_name="my-service",
        # Set these explicitly — the implicit default is deprecated and will
        # flip to False. False is the safe choice: request/response bodies
        # are user-supplied content that field-name redaction cannot
        # reliably scrub. Opt in deliberately, per service, only when you
        # know the bodies are safe to persist.
        log_request_body=False,
        log_response_body=False,
    ),
)

logger = get_logger(__name__)

@app.get("/")
async def root():
    logger.info("Hello World")
    return {"message": "Hello World"}
```

### Quart

```python
from quart import Quart
from auditry import configure_logging, ObservabilityConfig, get_logger
from auditry.quart import create_middleware

# Configure structured logging at startup
configure_logging(
    level="INFO",
    service="my-service",
    version="1.0.0",
    environment="prod",
)

app = Quart(__name__)

# Add observability middleware (single line!)
app = create_middleware(
    app,
    config=ObservabilityConfig(
        service_name="my-service",
        log_request_body=False,   # set explicitly; False is the safe choice
        log_response_body=False,
    ),
)

logger = get_logger(__name__)

@app.route("/")
async def root():
    logger.info("Hello World")
    return {"message": "Hello World"}
```

## Configuration

### Required Configuration

```python
config = ObservabilityConfig(
    service_name="your-service-name",  # REQUIRED
)
```

**Note:** The `service_name` is required and must be provided. This ensures all services have meaningful names in logs rather than generic defaults.

### Full Configuration Options

```python
config = ObservabilityConfig(
    # REQUIRED: Service name for log filtering (no default)
    service_name="my-service-name",

    # Request ID header name (default: X-Request-ID)
    # Override if your org uses a different header
    correlation_id_header="X-Request-ID",

    # Maximum request/response body size to log (default: 10KB)
    payload_size_limit=10_240,

    # Additional sensitive field patterns to redact
    additional_redaction_patterns=["internal_id", "employee_ssn"],

    # Whether to log request headers (default: True)
    log_request_headers=True,

    # Whether to log response headers (default: False)
    log_response_headers=False,

    # Whether to log query parameters (default: True)
    log_query_params=True,

    # Whether to log request bodies (default today: True, deprecated — will
    # flip to False). Bodies are user-supplied content that field-name
    # redaction cannot reliably scrub; set False unless you know the bodies
    # on every endpoint are safe to persist.
    log_request_body=False,

    # Whether to log response bodies (same caveat as request bodies)
    log_response_body=False,
)

# For FastAPI:
from auditry.fastapi import create_middleware
app = create_middleware(app, config)

# For Quart:
from auditry.quart import create_middleware
app = create_middleware(app, config)
```

## Request / Correlation IDs

Request IDs are automatically handled:

- **Incoming requests**: Extracts from `X-Request-ID` header (or your custom header)
- **Generated if missing**: Creates a new UUID if no request ID provided
- **Added to response**: Returns the request ID in the response header
- **Included in logs**: Automatically included in all structured logs

One nuance: `correlation_id` is an **optional** field of the root schema. It is
present whenever a context is bound — every line inside a request, and every
line inside a worker task that binds one — and absent on lines logged outside
any context (import time, startup hooks). Consumers should treat the field as
optional rather than assuming it on every line; auditry deliberately does not
invent a per-line fallback ID, because each line would get a *different* ID,
which falsely implies correlation.

### Using Correlation IDs in Your Code

```python
from auditry import get_logger, get_correlation_id

logger = get_logger(__name__)

@app.get("/users/{user_id}")
async def get_user(user_id: str):
    # Correlation ID is automatically available
    correlation_id = get_correlation_id()

    # All logs automatically include the correlation ID
    logger.info(f"Fetching user {user_id}")

    return {"user_id": user_id, "correlation_id": correlation_id}
```

### Propagating to Downstream Services

`outbound_headers()` builds the headers for you, generating an ID if none is
bound yet so an outbound call is never made without one:

```python
import httpx
from auditry import outbound_headers

@app.get("/proxy")
async def proxy_request():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://downstream-service.com/api/data",
            headers=outbound_headers(),          # {"X-Request-ID": "<id>"}
        )

    return response.json()
```

Pass `extra=` to merge with headers you were already sending, and `header_name=`
if your organization uses a different header. Attach these to third-party API
calls too, wherever the SDK accepts custom headers — it makes the vendor's
audit trail line up with yours.

## Correlation Propagation Beyond HTTP

The middleware binds the correlation ID for HTTP requests. Everything outside
that request cycle — queue workers, schedulers, cron jobs, scripts — has to bind
one itself, **before the first log line**, or those logs correlate with nothing.

### Workers and Background Jobs

```python
from auditry import configure_logging, get_logger, with_correlation

# Workers too, not just the API. version falls back to SERVICE_VERSION if unset.
configure_logging(service="my-worker", version="1.4.2", environment="prod")
logger = get_logger(__name__)

@with_correlation
async def process_job(ctx, job_spec, correlation_id=None):
    # The ID is bound before this body runs, so every line below carries it.
    logger.info("job started", job_type=job_spec["type"])
```

The decorator takes the ID from a `correlation_id` keyword argument when the
producer passes one, and generates a fresh UUID4 otherwise. It leaves the kwarg
in place if your function accepts it and strips it if not, so you can decorate
functions that never declared it.

For manual control, use `bind_correlation_id(value)` (propagate an inbound ID —
never regenerate mid-chain) or `ensure_correlation_id()` (return the current ID,
binding a fresh one if unset).

### Queue Hops (SQS / SNS)

```python
from auditry import bind_from_sqs_message, sqs_message_attributes

# Producer
sqs.send_message(
    QueueUrl=queue_url,
    MessageBody=json.dumps(job),
    MessageAttributes=sqs_message_attributes(),
)

# Consumer — call this BEFORE the first log line
for message in response["Messages"]:
    bind_from_sqs_message(message)
    logger.info("processing message")
```

`bind_from_sqs_message` falls back to a fresh ID when the attribute is absent,
so a consumer never logs without one.

IDs that auditry *generates* are always random UUID4s — opaque, never derived
from user data. Inbound IDs are propagated verbatim (that is the point of
propagation), so the randomness guarantee holds end-to-end only when the
edge service generated the ID. Don't accept correlation IDs from untrusted
callers into systems that assume opacity.

## Metrics (CloudWatch EMF)

`MetricsLogger` emits CloudWatch [Embedded Metric Format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html):
a metric is a structured JSON log line that CloudWatch extracts at ingestion.
That means **no AWS SDK dependency, no network call on the request path, and no
credentials to manage** — metrics ride the log driver you already have.

```python
from auditry import MetricsLogger

metrics = MetricsLogger(namespace="MyOrg/MyService", service="my-service")

metrics.count("JobsSubmitted")                       # +1
metrics.count("JobsSubmitted", 5)                    # +5
metrics.timing("RenderLatency", 42.7)                # milliseconds
metrics.zero("RateLimited")                          # see below
```

### Timing Dependency Calls

`dependency_call()` times a call and records success or failure, with the
exception class as an `ErrorType` dimension:

```python
with metrics.dependency_call("dynamodb", resource="jobs-table"):
    table.get_item(Key={"id": job_id})
```

That emits `Latency` (ms), `Success`, and `Error` under a `Dependency`
(plus optional `Resource`) dimension. A success emits `Error: 0` and a failure
emits `Success: 0`, so neither series ever goes silent. The exception
propagates unchanged — this measures, it doesn't swallow.

### Why `zero()` Exists

Monitoring systems forget metrics that stop reporting. If you only emit
`RateLimited` when rate limiting happens, then "no data" and "no problem" look
identical, and an alarm on that metric can never fire reliably. Emitting an
explicit zero on the healthy path keeps the series alive so "no data" alarms
work.

### Dimensions Are Guarded

Dimension names are validated against a forbidden-pattern list (`userid`,
`email`, `filename`, `prompt`, `message`, and similar), and a match raises
`ForbiddenDimensionError` **instead of emitting**:

```python
metrics.count("Uploads", dimensions={"user_email": email})   # ForbiddenDimensionError
metrics.count("Uploads", dimensions={"FileType": "pdf"})     # fine
```

This is deliberately louder than the equivalent mistake in a log line. Metrics
stores are unencrypted, broadly readable, and not selectively erasable — you
cannot delete one user's data out of a metric after the fact. Failing at the
call site is cheaper than discovering it in review, or not at all.

There is also a hard cap of 8 dimensions per record (`ValueError`), since every distinct
dimension set is a separately billable metric.

### Rollup Dimension Sets

An error metric dimensioned by error type is unalarmable on its own: an alarm on
the coarse series finds no data, and you cannot enumerate every exception class
a dependency might raise. So `dependency_call()` records each error under
**both** `[Service, Dependency, ErrorType]` and `[Service, Dependency]` — one
record, no double counting within a set. Alarm on the coarse series, then use
the fine one to see which error type drove it.

When `resource=` is passed, the fine set gains a `Resource` dimension but the
rollup stays `[Service, Dependency]`, and successes roll up there too — so the
per-dependency alarm series always has data (`Error: 0` between failures), no
matter how the calls are scoped.

`emit()` exposes the same mechanism directly:

```python
metrics.emit(
    {"Error": 1},
    dimensions={"Dependency": "s3", "ErrorType": "ClientError"},
    rollup_dimension_sets=[["Dependency"]],   # also record under [Service, Dependency]
)
```

Rollup names must already be present on the record, or `emit()` raises
`ValueError`.

## User Tracking

The middleware automatically extracts user IDs from your authentication system and includes them in logs.

### FastAPI User Tracking

```python
from fastapi import Request, Depends

async def get_current_user(request: Request):
    # Your authentication logic here
    user_id = "user-123"

    # Set user_id in request state for auditry to capture
    request.state.user_id = user_id
    # OR if you have a user object:
    # request.state.user = user_object  # Must have .id or .user_id attribute

    return user_id

@app.get("/protected")
async def protected_route(user_id: str = Depends(get_current_user)):
    return {"message": "Protected content"}
```

### Quart User Tracking

```python
from quart import request

@app.before_request
async def authenticate():
    # Your authentication logic here

    # Set user on request for auditry to capture
    request.current_user = AuthenticatedUser(id="user-123")
    # OR use g.user or g.user_id
    # from quart import g
    # g.user_id = "user-123"
```

The middleware automatically finds the user ID from these locations:
- **FastAPI**: `request.state.user_id` or `request.state.user.id`
- **Quart**: `request.current_user.id`, `g.user.id`, or `g.user_id`

## Business Event Tagging (For Analytics)

Tag specific endpoints as "business events" to make analytics queries easier for your sales/product teams.

### Configuration

Tag endpoints in your middleware config - zero code changes needed in your actual endpoints:

```python
from auditry import ObservabilityConfig, BusinessEventConfig

config = ObservabilityConfig(
    service_name="my-service-name",

    # Define which endpoints to tag for analytics
    business_events={
        "POST /workflows": BusinessEventConfig(
            event_type="workflow.created",
            extract_from_request=["file_id"],  # Pull file_id from request body
            extract_from_response=["id"],       # Pull workflow id from response
        ),
        "DELETE /workflows/{workflow_id}": BusinessEventConfig(
            event_type="workflow.deleted",
            extract_from_path=["workflow_id"],    # Pull workflow_id from URL path
        ),
    },
)

# Apply to your framework
from auditry.fastapi import create_middleware  # or auditry.quart
app = create_middleware(app, config)
```

### Log Output with Event Tags

Regular log (no tagging):
```json
{
  "service": "my-service-name",
  "message": "Request completed: POST /workflows - Status: 201",
  "request": {...},
  "response": {...}
}
```

Tagged business event log:
```json
{
  "service": "my-service-name",
  "message": "Request completed: POST /workflows - Status: 201",
  "event_type": "workflow.created",          // ← Filterable in log platform
  "business_context": {
    "file_id": "file_123",                   // ← From request body
    "id": "workflow_789"                    // ← From response body
  },
  "request": {...},
  "response": {...}
}
```

### Supported Extract Locations

- `extract_from_request`: Fields from request JSON body
- `extract_from_response`: Fields from response JSON body  
- `extract_from_path`: Parameters from URL path (e.g., `/workflows/{workflow_id}`)

## Log Output

All logs are structured JSON, ready for log aggregators:

```json
{
  "timestamp": "2025-10-28T12:34:56.789012+00:00",
  "level": "INFO",
  "service": "my-service-name",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Request completed: POST /workflows - Status: 201 - Duration: 45.23ms",
  "request": {
    "method": "POST",
    "path": "/workflows",
    "query_params": {},
    "headers": {"user-agent": "curl/7.64.1", "authorization": "[REDACTED]"},
    "body": {"name": "My Workflow", "password": "[REDACTED]"},
    "user_id": "user_12345"
  },
  "response": {
    "status_code": 201,
    "duration_ms": 45.23,
    "body": {"id": "workflow_789", "name": "My Workflow"}
  }
}
```

## Sensitive Data Handling

### Automatic Redaction

Automatically redacts these sensitive field patterns in all logged request
bodies, response bodies, headers, **and query parameters**:

- `password` / `passwd`
- `token` / `jwt` / `bearer`
- `api_key` / `apikey` / `x-api-key`
- `access_key` / `private_key`
- `secret` / `credential`
- `authorization` / `proxy-authorization` / `x-auth`
- `cookie` / `set-cookie`
- `otp` / `one_time_password`
- `signature`
- `csrf` / `xsrf`
- `ssn` / `social_security_number`
- `credit_card` / `creditcard`
- `x-amz-security-token`

Add custom patterns via configuration:

```python
config = ObservabilityConfig(
    service_name="my-service-name",
    additional_redaction_patterns=["internal_token", "employee_id"],
)
```

### Disabling Body Logging (Application-Wide)

For applications handling sensitive data, you can disable logging of request and/or response bodies across the entire application:

```python
config = ObservabilityConfig(
    service_name="my-service-name",

    # Disable request body logging for all endpoints
    log_request_body=False,

    # Disable response body logging for all endpoints
    log_response_body=False,
)
```

When body logging is disabled, logs will show `[BODY_LOGGING_DISABLED]` instead of the actual content, while still logging metadata like headers, status codes, and timing information.

Both flags still default to `True`, but leaving them implicit now raises a
`DeprecationWarning` — **the defaults will flip to `False` in a future release.**
Bodies are user-supplied content, and field-name redaction cannot reliably scrub
free text: a prompt, an uploaded document, or a generated completion has no
field names to match on. Setting either flag explicitly (even to `True`) is
treated as a deliberate choice and silences the warning.

### Exception Details

Error logs are the single most likely place for customer content to leak.
Exception messages routinely interpolate exactly the input that caused the
failure — a parse error quotes the document, a validation error quotes the field
value, an SDK error quotes the payload. Redaction cannot help, because a
traceback has no field names to match.

So as of 0.4.0, a failed request logs the exception **class** and the
correlation ID, and nothing else:

```json
{
  "level": "ERROR",
  "error_type": "ValueError",
  "exception_type": "ValueError",
  "correlation_id": "abc-123",
  "message": "Request failed: POST /workflows"
}
```

You debug by taking the correlation ID and searching your logs, rather than by
reading the exception text off the error line.

Three escape hatches, in increasing order of exposure:

```python
# 1. Route full tracebacks to a destination you control the access to.
import logging

from auditry import set_trace_handler

# A dedicated logger with its OWN handler, shipping to an encrypted,
# access-controlled destination. propagate=False keeps it off the root
# handler — never write traces back to stdout; that defeats the point.
secure_logger = logging.getLogger("app.secure-traces")
secure_logger.propagate = False
secure_logger.addHandler(logging.FileHandler("/var/log/secure/traces.log"))

def route_to_secure_log(error_type, traceback_text, event_dict):
    secure_logger.error(
        "%s correlation_id=%s\n%s",
        error_type, event_dict.get("correlation_id"), traceback_text,
    )

set_trace_handler(route_to_secure_log)
```

`set_trace_handler` is the seam that lets traces go somewhere access-controlled
without auditry needing to know anything about that destination — a
restricted-access log group, an error tracker, whatever you use. Pass `None` to
remove the handler.

The handler receives the exception **class name** and a rendered traceback
**string**, not an `exc_info` tuple — so it can write traces anywhere that
accepts text, but it cannot re-raise or re-capture the original exception
object. If the handler itself raises, auditry swallows it and marks the line
`trace_handler_error: true` rather than letting your logging path break the
request.

```python
# 2. Put exception messages back on the standard stream, per service.
config = ObservabilityConfig(
    service_name="my-service",
    log_exception_messages=True,   # default False
)
```

```python
# 3. Local development only: inline full tracebacks (flattened to one line).
#    AUDITRY_FULL_TRACEBACKS=true
```

### Excluding Paths

Skip logging for specific endpoints like health checks or streaming:

```python
# Basic exclusion
config = ObservabilityConfig(
    service_name="my-service-name",
    excluded_paths=[
        '/health',           # Exact match
        '/metrics',          # Exact match
        '/stream*',          # Wildcard - matches /stream, /streaming, /stream/events
        '/api/*/internal',   # Wildcard - matches /api/v1/internal, /api/v2/internal
        '/admin/',           # Prefix - matches /admin/* (trailing slash indicates prefix)
    ],
)

# Method-specific
config = ObservabilityConfig(
    service_name="my-service",
    excluded_paths={
        'GET': ['/health'],
        'POST': ['/webhook/*'],
        '*': ['/admin/*']  # all methods
    }
)
```

Useful for streaming endpoints that don't play nice with middleware:

```python
from quart import Quart, Response, stream_with_context
from auditry import ObservabilityConfig
from auditry.quart import create_middleware

app = Quart(__name__)
config = ObservabilityConfig(
    service_name="stream-svc",
    excluded_paths=['/stream/*']  # skip these
)
app = create_middleware(app, config)

@app.route('/stream/data')
async def stream_data():
    # this won't be logged
    @stream_with_context
    async def generate():
        for i in range(100):
            yield f"data: {i}\n\n"
    return Response(generate(), mimetype='text/event-stream')
```

Note: Excluded paths still get correlation IDs but no logging.

### Health Probes Are Excluded by Default

As of 0.4.0, these paths are merged into `excluded_paths` automatically:

```text
/health  /healthz  /livez  /live  /ready  /readyz  /api/health
```

A load balancer probing every task every 15–30 seconds dominates log volume in
most deployed services, and those lines carry no information. Probes still
receive the correlation-ID header — they just stop producing request/response
log lines. Opt out with:

```python
config = ObservabilityConfig(
    service_name="my-service",
    include_default_excluded_paths=False,
)
```

Expect log-derived request counts to drop after upgrading, sometimes sharply.
If you have an alarm on low log volume, check it before you roll this out.

## Best Practices

### 1. Configure Logging Early

Call `configure_logging()` at application startup, before any other code:

```python
from auditry import configure_logging

# First thing in your app
configure_logging(level="INFO")

app = FastAPI()
# ... rest of your app
```

### 2. Use Structured Logging

Prefer `get_logger(__name__)` over standard Python logging:

```python
from auditry import get_logger

logger = get_logger(__name__)

# Good - structured key/value fields, queryable individually
logger.info("Processing payment", amount=100.50, currency="USD")

# Works, but flat - the line still carries the JSON root schema
# (timestamp, service, correlation ID), but the data is baked into
# the message string instead of being queryable fields
import logging
logging.info("Processing payment amount=%s", 100.50)
```

Plain-stdlib records — including those from libraries you don't control
(uvicorn, boto3) — are rendered through the same processor chain, so every
line on stdout is schema-carrying JSON either way. `get_logger` is about
making *your* fields structured and queryable.

### 3. Propagate Correlation IDs

When calling downstream services, always pass the correlation ID —
`outbound_headers()` builds the headers and guarantees an ID is present
(binding a fresh one if none exists yet), so don't assemble them by hand:

```python
from auditry import outbound_headers

response = await client.get(url, headers=outbound_headers())

# If your org uses a different header name, or you have headers already:
response = await client.get(
    url,
    headers=outbound_headers(header_name="X-Trace-ID", extra={"Accept": "application/json"}),
)
```

### 4. Customize for Your Organization

Match your org's conventions:

```python
config = ObservabilityConfig(
    service_name="my-service-name",
    correlation_id_header="X-Trace-ID",  # If your org uses a different header
    additional_redaction_patterns=["ssn", "tax_id"],  # Your sensitive fields
)
```

## Example Output: Success vs Failure

### Successful Request

```json
{
  "level": "INFO",
  "service": "my-service-name",
  "correlation_id": "abc-123",
  "message": "Request completed: POST /workflows - Status: 201 - Duration: 45ms",
  "request": {...},
  "response": {...}
}
```

### Failed Request

```json
{
  "level": "ERROR",
  "service": "my-service-name",
  "version": "1.4.2",
  "environment": "prod",
  "correlation_id": "abc-123",
  "message": "Request failed: POST /workflows",
  "request": {...},
  "error_type": "ValueError",
  "exception_type": "ValueError",
  "execution_duration_ms": 12.34
}
```

Note what is *not* there: no traceback, and no `exception_message`. See
[Exception Details](#exception-details) for why, and for how to get them back
when you need them.

## Migration Guide: 0.3.x to 0.4.0

Nothing breaks at import or construction time — no exports were removed, no
signature changed incompatibly, and every new `ObservabilityConfig` field has a
default. You can bump the version without touching code and the app will run —
with one caveat: a test suite that promotes `DeprecationWarning` to an error
(`-W error`, `filterwarnings = ["error"]`) will fail on `ObservabilityConfig`
construction until `log_request_body` / `log_response_body` are set explicitly
(breaking-change item 6 below).

What changes is **what the logs look like**, so the breakage shows up in the
tooling that reads them rather than in your service.

### Breaking Changes

1. **The message key is now `message`, not `event`.**

   This is the one that actually bites. Any saved Logs Insights query,
   CloudWatch metric filter, or dashboard referencing `event` stops matching —
   and **metric-filter alarms fail silently**, because a filter that matches
   nothing looks exactly like a healthy service. Audit your metric filters
   before upgrading any service with alarms on log patterns.

2. **Error logs no longer carry tracebacks or `exception_message`.**

   They carry `error_type` and the correlation ID instead. `exception_type` is
   unchanged, so dashboards keyed on it keep working. See
   [Exception Details](#exception-details) to opt back in.

3. **Health-probe requests are no longer logged.** Log-derived request counts
   will drop. See [Health Probes Are Excluded by Default](#health-probes-are-excluded-by-default).

4. **Query parameters are now redacted**, along with 14 additional field
   patterns. Anything parsing a value out of a field named `signature` or
   `credential` will now find `[REDACTED]`.

5. **Timestamps are explicitly UTC** where they previously followed the
   container's local time, and stdlib logging is explicitly bound to **stdout**
   where `basicConfig` defaulted to stderr. Both are no-ops under the awslogs
   driver; both matter if anything downstream separates the streams or parses
   local timestamps.

6. **A `DeprecationWarning` fires on construction** if `log_request_body` /
   `log_response_body` are left implicit. A test suite running with `-W error`
   or `filterwarnings = ["error"]` will fail until you set them explicitly.
   That is the intended nudge, but it surfaces as a CI failure, not a log line.

### Upgrade Steps

1. Pass `service` / `version` / `environment` to `configure_logging()` — or set
   `SERVICE_NAME` / `SERVICE_VERSION` / `ENVIRONMENT`. Do this in **worker
   entrypoints too**, not just the API.

   ```python
   configure_logging(service="my-service", version="1.4.2", environment="prod")
   ```

2. Set `log_request_body` and `log_response_body` explicitly. Services handling
   customer content should choose `False`.

3. Update saved Logs Insights queries and metric filters: `event` → `message`.

4. In workers, bind a correlation ID before the first log line, and add
   `outbound_headers()` to outbound calls. See
   [Correlation Propagation Beyond HTTP](#correlation-propagation-beyond-http).

5. Decide how you want tracebacks handled: register `set_trace_handler(...)` to
   route them to a gated destination, or accept `error_type` only.

### New Features

- **`auditry.metrics.MetricsLogger`** — dependency-free CloudWatch EMF emitter
  with dependency-call timing, per-error-reason counts, zero-count support for
  no-data alarms, and validation that rejects user content in dimensions.
  See [Metrics (CloudWatch EMF)](#metrics-cloudwatch-emf).
- **`auditry.propagation`** — correlation IDs across workers, outbound HTTP, and
  SQS/SNS hops.
- **`set_trace_handler(...)`** — route full tracebacks to a destination you
  control access to.
- **Root log schema** — `service` / `version` / `environment` on every line, and
  the correlation ID attached to *every* line rather than only middleware ones.

## Migration Guide: 0.2.x to 0.3.0

### Breaking Changes

1. **Default header changed from `X-Correlation-ID` to `X-Request-ID`**

   All services should upgrade to 0.3.0 together so they consistently use `X-Request-ID`. If you need to do a phased rollout, you can temporarily pin the old header on already-upgraded services:

   ```python
   config = ObservabilityConfig(
       service_name="my-service",
       correlation_id_header="X-Correlation-ID",  # Temporary: remove once all services are on 0.3.0
   )
   ```

### New Features

- **Streaming-friendly middleware** — Safer handling of streaming responses so large bodies are not buffered for logging when not appropriate.
- **`excluded_paths`** — Skip request/response logging for configured paths (for example health checks or long-lived streams) while still attaching the request ID header.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions welcome! Please submit a Pull Request.

## Support

For issues and questions: [GitHub Issues](https://github.com/farsight-ai/auditry/issues)
