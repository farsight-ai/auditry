"""auditry without the otel extra: everything except tracing still works.

Runs in a subprocess with OpenTelemetry blocked, because this process has it
installed and already imported."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

NO_OTEL_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import contextlib
    import io
    import json
    import sys

    sys.modules["opentelemetry"] = None  # every opentelemetry.* import now fails

    import auditry
    from auditry import (
        ObservabilityConfig,
        bound_message_context,
        configure_logging,
        get_logger,
        outbound_headers,
        sqs_message_attributes,
    )
    from auditry.span_scrubbing import SpanScrubPolicy, scrub_attributes

    results = {"version": auditry.__version__}

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        configure_logging(service="svc", environment="prod")
        get_logger("t").info("hello")
    results["log_line"] = json.loads(out.getvalue().splitlines()[-1])

    results["headers"] = outbound_headers()
    attributes = sqs_message_attributes()
    results["sqs_attributes"] = sorted(attributes)
    with bound_message_context({"MessageAttributes": attributes}) as cid:
        results["bound_cid"] = cid == attributes["correlation_id"]["StringValue"]
    results["scrubbed"] = scrub_attributes(
        {"user.id": "u"}, is_server=True, policy=SpanScrubPolicy()
    )

    try:
        import auditry.otel
    except ImportError:
        results["otel_import"] = "ImportError"

    def config(instrument_server):
        return ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            tracing={"instrument_server": instrument_server},
        )

    def require_otel_error(create, app):
        try:
            create(app, config(True))
        except ImportError as exc:
            return str(exc)
        return None

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auditry.fastapi import create_middleware as fastapi_middleware

    app = FastAPI()
    app.get("/")(lambda: {"ok": True})
    with contextlib.redirect_stdout(io.StringIO()):
        response = TestClient(fastapi_middleware(app, config(False))).get("/")
    results["fastapi_status"] = response.status_code
    results["fastapi_error"] = require_otel_error(fastapi_middleware, FastAPI())

    from quart import Quart
    from auditry.quart import create_middleware as quart_middleware

    quart_app = Quart(__name__)

    @quart_app.route("/")
    async def index():
        return {"ok": True}

    async def quart_get():
        with contextlib.redirect_stdout(io.StringIO()):
            return (await quart_middleware(quart_app, config(False)).test_client().get("/"))

    results["quart_status"] = asyncio.run(quart_get()).status_code
    results["quart_error"] = require_otel_error(quart_middleware, Quart(__name__))

    print(json.dumps(results))
    """
)

REQUIRE_OTEL_MESSAGE = (
    "tracing.instrument_server requires the 'otel' extra: pip install 'auditry[otel]'"
)


@pytest.fixture(scope="module")
def results():
    pytest.importorskip("fastapi")
    pytest.importorskip("quart")
    completed = subprocess.run(
        [sys.executable, "-c", NO_OTEL_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "AUDITRY_STRICT": "0"},
        timeout=25,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_auditry_imports(results):
    assert results["version"]


def test_log_line_without_trace_fields(results):
    line = results["log_line"]
    assert line["message"] == "hello"
    assert "trace_id" not in line
    assert "span_id" not in line


def test_propagation_helpers_carry_no_trace_context(results):
    assert set(results["headers"]) == {"X-Request-ID"}
    assert results["sqs_attributes"] == ["correlation_id"]
    assert results["bound_cid"] is True


def test_span_scrubbing_rules_need_no_otel(results):
    assert results["scrubbed"] == {}


def test_otel_subpackage_needs_the_extra(results):
    assert results["otel_import"] == "ImportError"


@pytest.mark.parametrize("framework", ["fastapi", "quart"])
def test_create_middleware_without_tracing(results, framework):
    assert results[f"{framework}_status"] == 200


@pytest.mark.parametrize("framework", ["fastapi", "quart"])
def test_instrument_server_names_the_extra(results, framework):
    assert results[f"{framework}_error"] == REQUIRE_OTEL_MESSAGE
