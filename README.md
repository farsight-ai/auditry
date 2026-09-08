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

    # Whether to log request bodies for all endpoints (default: True)
    # Set to False for applications handling sensitive data
    log_request_body=True,

    # Whether to log response bodies for all endpoints (default: True)
    # Set to False for applications returning sensitive data
    log_response_body=True,
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
  "correlation_id": "abc-123",
  "message": "Request failed: POST /workflows"
}
```

You debug by taking the correlation ID and searching your logs, rather than by
reading the exception text off the error line.

Three escape hatches, in increasing order of exposure:

```python
# 1. Route full exception details to a destination you control the access to.
import logging
import traceback

from auditry import set_trace_handler

# An error tracker gets the live exception object:
set_trace_handler(
    lambda error_type, exc_info, event_dict: sentry_sdk.capture_exception(exc_info[1])
)

# Or a dedicated logger with its OWN handler, shipping to an encrypted,
# access-controlled destination. propagate=False keeps it off the root
# handler — never write traces back to stdout; that defeats the point.
secure_logger = logging.getLogger("app.secure-traces")
secure_logger.propagate = False
secure_logger.addHandler(logging.FileHandler("/var/log/secure/traces.log"))

def route_to_secure_log(error_type, exc_info, event_dict):
    secure_logger.error(
        "%s correlation_id=%s\n%s",
        error_type,
        event_dict.get("correlation_id"),
        "".join(traceback.format_exception(*exc_info)),
    )

set_trace_handler(route_to_secure_log)
```

`set_trace_handler` is the seam that lets traces go somewhere access-controlled
without auditry needing to know anything about that destination — a
restricted-access log group, an error tracker, whatever you use. Pass `None` to
remove the handler.

The handler receives `(error_type, exc_info, event_dict)`: the exception class
name, the live `(type, value, traceback)` tuple, and a snapshot of the line's
fields under the root schema (`message`, `correlation_id`, `service`, …). Render
the tuple to text yourself if you need text. If the handler raises, auditry
swallows it and marks the line `trace_handler_error: true` rather than letting
your logging path break the request.

This discipline applies to **auditry's own records** — the middleware's
request/response lines and anything logged through `get_logger()`. Plain stdlib
loggers (`logging.getLogger(...)` in your code or a vendor SDK) share the root
schema but **keep their tracebacks**, in the JSON-escaped `exception` field:
auditry does not delete the stack trace of every `except` block in your process.

```python
# 2. Put exception messages back on the standard stream, per service.
config = ObservabilityConfig(
    service_name="my-service",
    log_exception_messages=True,   # default False
)
```

```python
# 3. Local development only: inline the full traceback in the `exception`
#    field (JSON-escaped, so the stream line stays single-line and the value
#    stays a real, tool-parseable traceback). Read once at configure_logging().
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

Always use `get_logger(__name__)` instead of standard Python logging:

```python
from auditry import get_logger

logger = get_logger(__name__)

# Good - structured with correlation ID
logger.info("Processing payment", amount=100.50, currency="USD")

# Bad - loses structured data
import logging
logging.info("Processing payment")
```

### 3. Propagate Correlation IDs

When calling downstream services, always pass the correlation ID:

```python
from auditry import get_correlation_id

correlation_id = get_correlation_id()
headers = {"X-Request-ID": correlation_id}  # Use your org's header name
response = await client.get(url, headers=headers)
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
  "execution_duration_ms": 12.34
}
```

Note what is *not* there: no traceback, and no `exception_message`. See
[Exception Details](#exception-details) for why, and for how to get them back
when you need them.

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
