"""log_route_template: a request line names the route that matched, not the concrete path, and
carries no path parameter values. Off, the line is as it always was."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quart import Quart

from auditry import ObservabilityConfig, configure_logging
from auditry.core.logger import UNKNOWN_ROUTE
from auditry.fastapi import create_middleware as create_fastapi_middleware
from auditry.quart import create_middleware as create_quart_middleware

pytestmark = pytest.mark.usefixtures("clean_logging")

CUSTOMER_NAME = "AcmeQ3Deal"
CONCRETE_PATH = f"/builds/{CUSTOMER_NAME}/files"


def configure(log_route_template: bool) -> ObservabilityConfig:
    """Configures logging, as a service does at startup, and returns the middleware's config.
    Called from the test body, once capsys holds stdout, so the lines land where log_lines reads
    them."""
    configure_logging(service="svc", environment="prod")
    return ObservabilityConfig(
        service_name="svc",
        log_request_body=False,
        log_response_body=False,
        log_route_template=log_route_template,
    )


def request_lines(log_lines) -> list[dict]:
    return [line for line in log_lines() if line["message"].startswith("Request ")]


def fastapi_app(log_route_template: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/builds/{build_id}/files")
    def files(build_id: str):
        return {}

    @app.get("/templates/{name:path}/thumbnail")
    def thumbnail(name: str):
        return {}

    @app.get("/builds/{build_id}/crash")
    def crash(build_id: str):
        raise RuntimeError("boom")

    return create_fastapi_middleware(app, configure(log_route_template))


def test_fastapi_off_logs_the_concrete_path(log_lines):
    TestClient(fastapi_app(log_route_template=False)).get(CONCRETE_PATH)

    (line,) = request_lines(log_lines)
    assert line["request"]["path"] == CONCRETE_PATH


def test_fastapi_logs_the_route_template_and_no_customer_name(log_lines):
    client = TestClient(fastapi_app(log_route_template=True))

    client.get(CONCRETE_PATH)
    client.get(f"/templates/{CUSTOMER_NAME}/q3/thumbnail")

    lines = request_lines(log_lines)
    assert [line["request"]["path"] for line in lines] == [
        "/builds/{build_id}/files",
        "/templates/{name}/thumbnail",
    ]
    assert lines[0]["message"].startswith("Request completed: GET /builds/{build_id}/files ")
    assert CUSTOMER_NAME not in str(lines)


def test_fastapi_names_the_route_of_a_failed_request(log_lines):
    client = TestClient(fastapi_app(log_route_template=True), raise_server_exceptions=False)

    client.get(f"/builds/{CUSTOMER_NAME}/crash")

    (line,) = request_lines(log_lines)
    assert line["message"].startswith("Request failed: GET /builds/{build_id}/crash ")
    assert line["request"]["path"] == "/builds/{build_id}/crash"
    assert CUSTOMER_NAME not in str(line)


def test_fastapi_names_a_route_under_a_mount_with_its_prefix(log_lines):
    inner = FastAPI()

    @inner.get("/builds/{build_id}")
    def build(build_id: str):
        return {}

    outer = FastAPI()
    outer.mount("/v1", inner)
    client = TestClient(create_fastapi_middleware(outer, configure(log_route_template=True)))

    client.get(f"/v1/builds/{CUSTOMER_NAME}")

    (line,) = request_lines(log_lines)
    assert line["request"]["path"] == "/v1/builds/{build_id}"


def test_fastapi_logs_unknown_route_for_a_request_no_route_matched(log_lines):
    TestClient(fastapi_app(log_route_template=True)).get(f"/nowhere/{CUSTOMER_NAME}")

    (line,) = request_lines(log_lines)
    assert line["request"]["path"] == UNKNOWN_ROUTE
    assert line["response"]["status_code"] == 404
    assert CUSTOMER_NAME not in str(line)


def quart_app(log_route_template: bool) -> Quart:
    app = Quart(__name__)

    @app.route("/builds/<build_id>/files")
    async def files(build_id: str):
        return {}

    return create_quart_middleware(app, configure(log_route_template))


@pytest.mark.asyncio
async def test_quart_off_logs_the_concrete_path_and_its_parameters(log_lines):
    await quart_app(log_route_template=False).test_client().get(CONCRETE_PATH)

    (line,) = request_lines(log_lines)
    assert line["request"]["path"] == CONCRETE_PATH
    assert line["request"]["path_params"] == {"build_id": CUSTOMER_NAME}


@pytest.mark.asyncio
async def test_quart_logs_the_route_rule_and_no_path_parameters(log_lines):
    client = quart_app(log_route_template=True).test_client()

    await client.get(CONCRETE_PATH)
    await client.get(f"/nowhere/{CUSTOMER_NAME}")

    lines = request_lines(log_lines)
    assert [line["request"]["path"] for line in lines] == [
        "/builds/<build_id>/files",
        UNKNOWN_ROUTE,
    ]
    assert all("path_params" not in line["request"] for line in lines)
    assert CUSTOMER_NAME not in str(lines)
