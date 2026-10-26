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
from auditry.logging_config import _SERVER_ACCESS_LOGGER, _SERVER_LOGGERS
from tests.process_state import reset_process_state

_SERVER_LOGGER_NAMES = (*_SERVER_LOGGERS, _SERVER_ACCESS_LOGGER)

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


def _restore_server_loggers() -> None:
    for name in _SERVER_LOGGER_NAMES:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.filters.clear()
        server_logger.propagate = True
        server_logger.setLevel(logging.NOTSET)


@pytest.fixture
def uvicorn_configured():
    """uvicorn's loggers as its CLI leaves them, put back to defaults afterwards."""
    reset_process_state()
    logging.config.dictConfig(_UVICORN_STYLE_CONFIG)
    yield
    _restore_server_loggers()
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


def test_a_later_app_that_keeps_the_access_log_gets_uvicorns_handler_back(uvicorn_configured):
    access_logger = logging.getLogger("uvicorn.access")
    uvicorns_handlers = list(access_logger.handlers)
    configure_logging(service="svc")

    _app()
    _app(suppress_server_access_log=False)

    assert (access_logger.handlers, access_logger.propagate, access_logger.filters) == (
        uvicorns_handlers,
        False,
        [],
    )


_GUNICORN_LOGGER_FOR = {"uvicorn.error": "gunicorn.error", "uvicorn.access": "gunicorn.access"}


@pytest.fixture
def gunicorn_handler():
    """gunicorn's loggers with one handler, emptied afterwards."""
    handler = logging.NullHandler()
    for gunicorn_name in _GUNICORN_LOGGER_FOR.values():
        logging.getLogger(gunicorn_name).handlers = [handler]
    yield handler
    for gunicorn_name in _GUNICORN_LOGGER_FOR.values():
        logging.getLogger(gunicorn_name).handlers = []


def test_gunicorns_handler_lists_keep_their_handlers(uvicorn_configured, gunicorn_handler):
    # gunicorn's UvicornWorker binds uvicorn's loggers to gunicorn's own handler lists.
    for uvicorn_name, gunicorn_name in _GUNICORN_LOGGER_FOR.items():
        logging.getLogger(uvicorn_name).handlers = logging.getLogger(gunicorn_name).handlers

    configure_logging(service="svc")
    _app()

    for gunicorn_name in _GUNICORN_LOGGER_FOR.values():
        assert (gunicorn_name, logging.getLogger(gunicorn_name).handlers) == (
            gunicorn_name,
            [gunicorn_handler],
        )


# A server started in code, `uvicorn.run(app)`, configures logging when it builds its Config:
# after the app, and so after create_middleware(). These run that order with uvicorn itself.


@pytest.fixture
def server_loggers_restored():
    """Starts from no logging configured and puts uvicorn's loggers back afterwards."""
    reset_process_state()
    yield
    _restore_server_loggers()
    reset_process_state()


def _access_lines_once_uvicorn_is_configured(
    capsys, app_config=None, earlier_app_configs=(), **server_config
) -> list[str]:
    """The output of an access line logged as uvicorn logs it, once uvicorn is configured."""
    configure_logging(service="svc")
    for earlier_app_config in earlier_app_configs:
        _app(**earlier_app_config)
    app = _app(**(app_config or {}))
    uvicorn.Config(app, **server_config)
    capsys.readouterr()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/hook?token=SECRET", "1.1", 200
    )
    captured = capsys.readouterr()
    return [line for line in (captured.out + captured.err).splitlines() if "SECRET" in line]


def test_uvicorns_default_log_config_cannot_bring_the_access_log_back(
    server_loggers_restored, capsys
):
    lines = _access_lines_once_uvicorn_is_configured(capsys)

    # uvicorn's config re-installed its handler; the filter still drops the line
    assert logging.getLogger("uvicorn.access").hasHandlers() is True
    assert lines == []


def test_log_config_none_keeps_the_access_log_off(server_loggers_restored, capsys):
    assert _access_lines_once_uvicorn_is_configured(capsys, log_config=None) == []
    assert logging.getLogger("uvicorn.access").hasHandlers() is False


def test_access_log_false_keeps_the_access_log_off(server_loggers_restored, capsys):
    assert _access_lines_once_uvicorn_is_configured(capsys, access_log=False) == []


def test_a_service_that_keeps_the_access_log_gets_uvicorns_line(server_loggers_restored, capsys):
    lines = _access_lines_once_uvicorn_is_configured(
        capsys, app_config={"suppress_server_access_log": False}
    )

    assert len(lines) == 1


def test_a_later_app_that_keeps_the_access_log_gets_uvicorns_line(server_loggers_restored, capsys):
    lines = _access_lines_once_uvicorn_is_configured(
        capsys, app_config={"suppress_server_access_log": False}, earlier_app_configs=[{}]
    )

    assert len(lines) == 1
