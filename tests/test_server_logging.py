"""uvicorn's lines join the JSON contract, and its access line gives way to the middleware's.

The loggers are configured here as uvicorn's default logging config leaves them before it
imports the app: plain-text handlers, propagation off.
"""

import json
import logging
import logging.config

import pytest
import uvicorn
from fastapi import FastAPI

from auditry import ObservabilityConfig, configure_logging
from auditry.fastapi import create_middleware
from tests.process_state import reset_process_state

_SERVER_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")

# The shape of uvicorn.config.LOGGING_CONFIG: handlers on `uvicorn` and `uvicorn.access`,
# neither propagating, and `uvicorn.error` reaching `uvicorn`'s handler.
_UVICORN_STYLE_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s: %(message)s"}},
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


@pytest.fixture
def uvicorn_configured():
    """uvicorn's loggers as its CLI leaves them, put back to defaults afterwards."""
    reset_process_state()
    logging.config.dictConfig(_UVICORN_STYLE_CONFIG)
    yield
    for name in _SERVER_LOGGER_NAMES:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
        server_logger.setLevel(logging.NOTSET)
    reset_process_state()


def _app(**config) -> FastAPI:
    app = FastAPI()
    create_middleware(
        app,
        ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False, **config
        ),
    )
    return app


def test_uvicorn_lines_render_as_json(uvicorn_configured, capsys):
    configure_logging(service="svc")

    logging.getLogger("uvicorn.error").info("Started server process [42]")

    [line] = capsys.readouterr().out.strip().splitlines()
    record = json.loads(line)
    assert (record["message"], record["service"]) == ("Started server process [42]", "svc")


def test_uvicorn_keeps_no_handler_of_its_own(uvicorn_configured):
    configure_logging(service="svc")

    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        assert (name, server_logger.handlers, server_logger.propagate) == (name, [], True)


def test_the_middleware_turns_uvicorns_access_log_off(uvicorn_configured):
    configure_logging(service="svc")

    _app()

    # uvicorn writes access lines only while this can reach a handler
    assert logging.getLogger("uvicorn.access").hasHandlers() is False


def test_the_access_log_stays_off_whichever_runs_first(uvicorn_configured):
    _app()
    configure_logging(service="svc")

    assert logging.getLogger("uvicorn.access").hasHandlers() is False


def test_a_service_can_keep_uvicorns_access_log(uvicorn_configured):
    configure_logging(service="svc")

    _app(suppress_server_access_log=False)

    assert logging.getLogger("uvicorn.access").hasHandlers() is True


# A server started in code, `uvicorn.run(app)`, configures logging when it builds its Config:
# after the app, and so after create_middleware(). These run that order with uvicorn itself.


@pytest.fixture
def server_loggers_restored():
    """Starts from no logging configured and puts uvicorn's loggers back afterwards."""
    reset_process_state()
    yield
    for name in _SERVER_LOGGER_NAMES:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
        server_logger.setLevel(logging.NOTSET)
    reset_process_state()


def _uvicorn_access_log_on(**server_config) -> bool:
    """Whether uvicorn writes access lines once it is configured after the app."""
    configure_logging(service="svc")
    app = _app()
    uvicorn.Config(app, **server_config)
    # uvicorn writes access lines only while this can reach a handler
    return logging.getLogger("uvicorn.access").hasHandlers()


def test_uvicorns_default_log_config_brings_the_access_log_back(server_loggers_restored):
    # The case the README warns about: uvicorn's own config re-installs the handler.
    assert _uvicorn_access_log_on() is True


def test_log_config_none_keeps_the_access_log_off(server_loggers_restored):
    assert _uvicorn_access_log_on(log_config=None) is False


def test_access_log_false_keeps_the_access_log_off(server_loggers_restored):
    assert _uvicorn_access_log_on(access_log=False) is False
