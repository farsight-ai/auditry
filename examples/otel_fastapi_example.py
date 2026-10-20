"""
OpenTelemetry tracing with auditry and FastAPI.

Requires: pip install 'auditry[fastapi,otel]'

Programmatic setup, scrubbed spans printed to stdout:

    python examples/otel_fastapi_example.py

Zero-code setup (also needs opentelemetry-distro); auditry's exporter is
selected by name:

    OTEL_TRACES_EXPORTER=auditry_console OTEL_LOGS_EXPORTER=none \\
        OTEL_METRICS_EXPORTER=none \\
        opentelemetry-instrument python examples/otel_fastapi_example.py

Either way, __main__ sends one request with a query string and one to a
route that raises. Neither span carries the file name, the email address,
the exception message, or the stack trace. To serve the app instead:
uvicorn examples.otel_fastapi_example:app
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from auditry import ObservabilityConfig, TracingConfig, configure_logging, get_logger
from auditry.fastapi import create_middleware
from auditry.otel import ScrubbingConsoleSpanExporter

configure_logging(service="otel-example", environment="local")

# opentelemetry-instrument installs a provider before this module runs; set one
# up only when nothing has.
if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
    provider = TracerProvider(resource=Resource.create({"service.name": "otel-example"}))
    provider.add_span_processor(BatchSpanProcessor(ScrubbingConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

app = FastAPI()
app = create_middleware(
    app,
    ObservabilityConfig(
        service_name="otel-example",
        log_request_body=False,
        log_response_body=False,
        # Skipped automatically when zero-code setup already instrumented the app.
        tracing=TracingConfig(instrument_server=True, query_param_allowlist=["page"]),
    ),
)

logger = get_logger(__name__)


@app.get("/files/{name}")
async def get_file(name: str, page: int = 1):
    logger.info("serving file")  # carries trace_id and span_id
    return {"page": page}


@app.get("/boom")
async def boom():
    raise ValueError("cannot open q3-board-deck.pdf for jane@example.com")


if __name__ == "__main__":
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/files/q3-board-deck.pdf?email=jane@example.com&page=2")
    client.get("/boom")
    trace.get_tracer_provider().force_flush()
