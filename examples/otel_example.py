"""
Tracing example: two services and a queue worker in one process.

Shows:
- configure_tracing plus ObservabilityConfig(tracing=True)
- the trace crossing an HTTP hop (outbound_headers) and a worker hop
  (job kwargs from trace_context into @with_correlation), with the worker's
  own span in the same trace
- trace_id and span_id on every log line
- redaction: the query-string token and the exception message never reach
  the exported spans

Run against a local Grafana + Tempo stack:

    docker run --rm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python examples/otel_example.py

then open http://localhost:3000, Explore, Tempo. Without
OTEL_EXPORTER_OTLP_ENDPOINT the spans print to stderr instead.
"""

import asyncio
import logging
import os
import sys

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from auditry import (
    ObservabilityConfig,
    configure_logging,
    ensure_correlation_id,
    get_logger,
    outbound_headers,
    trace_context,
    with_correlation,
)
from auditry.fastapi import create_middleware
from auditry.otel import build_tracer_provider, configure_tracing

ENVIRONMENT = "local"

configure_logging(service="orders-api", environment=ENVIRONMENT)
logger = get_logger(__name__)
# httpx logs every request URL, query string included, at INFO. auditry does
# not redact other libraries' log records, so keep that logger at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)


def span_exporter():
    # None selects the default OTLP exporter, configured by the
    # OTEL_EXPORTER_OTLP_* env vars.
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    return ConsoleSpanExporter(out=sys.stderr)


def observability_config(service_name):
    return ObservabilityConfig(
        service_name=service_name,
        log_request_body=False,
        log_response_body=False,
        tracing=True,
    )


# Each service gets its own provider so they show up as two services in the
# trace view. orders-api's is the global one. Because both run in one process,
# inventory-api's span is recorded as INTERNAL; as a separate service it is
# a SERVER span.
orders_tracing = configure_tracing(
    service="orders-api", environment=ENVIRONMENT, exporter=span_exporter()
)
inventory_tracing = build_tracer_provider(
    service="inventory-api", environment=ENVIRONMENT, exporter=span_exporter()
)

# A stand-in for a job queue: each job is the kwargs for fulfil_order.
queue: asyncio.Queue[dict] = asyncio.Queue()

# create_middleware sets the process-wide service name on log lines, so the
# downstream service is created first and orders-api, created last, wins.
inventory = FastAPI()
create_middleware(
    inventory, observability_config("inventory-api"), tracer_provider=inventory_tracing
)


@inventory.post("/reserve/{sku}")
async def reserve(sku: str):
    logger.info("stock reserved", sku=sku)
    return {"sku": sku, "reserved": True}


orders = FastAPI()
create_middleware(orders, observability_config("orders-api"))

inventory_client = httpx.AsyncClient(
    transport=httpx.ASGITransport(app=inventory), base_url="http://inventory"
)


@orders.post("/orders/{sku}")
async def create_order(sku: str):
    # outbound_headers() carries the correlation ID and the traceparent.
    await inventory_client.post(f"/reserve/{sku}", headers=outbound_headers())
    await queue.put({"sku": sku, "correlation_id": ensure_correlation_id(), **trace_context()})
    return {"sku": sku, "status": "accepted"}


@orders.get("/orders/{order_id}")
async def get_order(order_id: str):
    return {"order_id": order_id}


@orders.get("/fail")
async def fail():
    # The message is customer data: logs and spans keep only "ValueError".
    raise ValueError("card 4111-1111-1111-1111 declined")


@with_correlation
async def fulfil_order(sku):
    # Runs in a CONSUMER span, child of the request that enqueued the job.
    logger.info("fulfilling order", sku=sku)


async def worker():
    while True:
        job = await queue.get()
        await fulfil_order(**job)
        queue.task_done()


async def main():
    worker_task = asyncio.create_task(worker())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=orders, raise_app_exceptions=False),
        base_url="http://orders",
    ) as client:
        await client.post("/orders/sku-123")
        await client.get("/orders/42?access_token=never-in-a-span")
        await client.get("/fail")
    await queue.join()
    worker_task.cancel()
    await inventory_client.aclose()
    orders_tracing.shutdown()
    inventory_tracing.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
