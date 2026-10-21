"""Conformance vectors: auditry's behavior recorded as data, for ports to other languages.

Regenerate conformance/contract.json and conformance/vectors.json from the code with

    .venv/bin/python -m tests.test_conformance

conformance/README.md documents the case format and the placeholders.
"""

import io
import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
import structlog
from asgi_correlation_id import correlation_id

from auditry.correlation import CORRELATION_ID_FIELD
from auditry.errors import AuditryError
from auditry.logging_config import (
    _FULL_TRACEBACKS_ENV,
    _STRICT_ENV,
    NON_PRODUCTION_ENVIRONMENTS,
    NON_PRODUCTION_PREFIXES,
    _set_config_service,
    _set_strict,
    configure_logging,
    get_logger,
    is_strict,
    set_trace_handler,
)
from auditry.metrics import (
    _EMF_MAX_KEYS_PER_DIMENSION_SET,
    _MAX_DIMENSION_VALUE_LEN,
    _MAX_DIMENSIONS,
    _RESERVED_RECORD_KEYS,
    FORBIDDEN_DIMENSION_PATTERNS,
    MetricsLogger,
)
from auditry.redaction import DEFAULT_REDACTION_PATTERNS, REDACTED_VALUE, redact_data
from auditry.tracing import TRACE_LOG_FIELDS

CONFORMANCE_DIR = Path(__file__).resolve().parent.parent / "conformance"
CONTRACT_PATH = CONFORMANCE_DIR / "contract.json"
VECTORS_PATH = CONFORMANCE_DIR / "vectors.json"

# Values that differ on every run are replaced by these placeholders, here and in a port.
TIMESTAMP_PLACEHOLDER = "<timestamp>"
EPOCH_MS_PLACEHOLDER = "<epoch-ms>"
DURATION_MS_PLACEHOLDER = "<duration-ms>"
ISO_8601_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$")
# The metric dependency_call() times; its value is a measured duration.
DEPENDENCY_LATENCY_METRIC = "Latency"

# Every variable a case may read. A case starts with all of them unset, so the
# developer's or the CI runner's environment never leaks into an expectation.
CASE_ENVIRONMENT_VARIABLES = (
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "ENVIRONMENT",
    _STRICT_ENV,
    _FULL_TRACEBACKS_ENV,
)


def build_contract() -> dict[str, Any]:
    """The tables a port reads instead of restating: every value comes from the code."""
    return {
        "log": {
            "correlation_id_field": CORRELATION_ID_FIELD,
            "trace_fields": list(TRACE_LOG_FIELDS),
            "full_tracebacks_environment_variable": _FULL_TRACEBACKS_ENV,
        },
        "strict": {
            "environment_variable": _STRICT_ENV,
            "non_production_environments": sorted(NON_PRODUCTION_ENVIRONMENTS),
            "non_production_prefixes": list(NON_PRODUCTION_PREFIXES),
        },
        "redaction": {
            "patterns": list(DEFAULT_REDACTION_PATTERNS),
            "marker": REDACTED_VALUE,
        },
        "metrics": {
            "forbidden_dimension_patterns": list(FORBIDDEN_DIMENSION_PATTERNS),
            "max_dimensions": _MAX_DIMENSIONS,
            "max_dimension_value_length": _MAX_DIMENSION_VALUE_LEN,
            "max_keys_per_dimension_set": _EMF_MAX_KEYS_PER_DIMENSION_SET,
            "reserved_record_keys": sorted(_RESERVED_RECORD_KEYS),
        },
    }


# ---------------------------------------------------------------------------
# Process state
# ---------------------------------------------------------------------------


def _reset_process_state() -> None:
    """Production policy, nothing bound, nothing configured."""
    correlation_id.set(None)
    structlog.contextvars.clear_contextvars()
    set_trace_handler(None)
    _set_config_service(None)
    _set_strict(False)
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


@contextmanager
def _case_environment(overrides: Mapping[str, str]) -> Iterator[None]:
    """Every variable in CASE_ENVIRONMENT_VARIABLES unset except the overrides."""
    names = set(CASE_ENVIRONMENT_VARIABLES) | set(overrides)
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _error_class(name: str) -> type[Exception]:
    """An exception class named as the case names it, so error_type has a known value."""
    return type(name, (Exception,), {})


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_line(line: dict[str, Any]) -> dict[str, Any]:
    """Replaces a well-formed timestamp with its placeholder; a malformed one stays and fails."""
    timestamp = line.get("timestamp")
    if isinstance(timestamp, str) and ISO_8601_UTC.match(timestamp):
        line["timestamp"] = TIMESTAMP_PLACEHOLDER
    return _normalize_record(line)


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    envelope = record.get("_aws")
    if isinstance(envelope, dict) and isinstance(envelope.get("Timestamp"), int):
        envelope["Timestamp"] = EPOCH_MS_PLACEHOLDER
    return record


def _normalize_duration(record: dict[str, Any]) -> dict[str, Any]:
    latency = record.get(DEPENDENCY_LATENCY_METRIC)
    if isinstance(latency, (int, float)) and latency >= 0:
        record[DEPENDENCY_LATENCY_METRIC] = DURATION_MS_PLACEHOLDER
    return record


# ---------------------------------------------------------------------------
# Case runners: one per kind, each returning the case's expected output
# ---------------------------------------------------------------------------


def run_redact(spec: dict[str, Any]) -> Any:
    return redact_data(spec["data"], spec.get("additional_patterns"))


def run_strict(spec: dict[str, Any]) -> bool:
    flag = spec.get("environment_variable")
    with _case_environment({_STRICT_ENV: flag} if flag is not None else {}):
        configure_logging(environment=spec.get("environment"), strict=spec.get("explicit"))
    return is_strict()


def _configure_case_logging(spec: dict[str, Any]) -> io.StringIO:
    """configure_logging() for the case, with stdout captured: its handler keeps the stream."""
    out = io.StringIO()
    with _case_environment(spec.get("environment_variables", {})), redirect_stdout(out):
        configure_logging(level=spec.get("level", "INFO"), **spec.get("configure", {}))
    correlation_id.set(spec.get("correlation_id"))
    return out


def _captured_lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [_normalize_line(json.loads(line)) for line in out.getvalue().splitlines()]


def run_log(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = _configure_case_logging(spec)
    logger = get_logger("conformance")
    for call in spec["calls"]:
        log_method = getattr(logger, call["level"])
        fields = call.get("fields", {})
        error_type = call.get("error_type")
        if error_type is None:
            log_method(call["message"], **fields)
            continue
        error_class = _error_class(error_type)
        error = error_class("an exception message never reaches the line")
        log_method(call["message"], exc_info=(error_class, error, None), **fields)
    return _captured_lines(out)


def run_metric_line(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """A metric record written through the configured log pipeline, not a sink."""
    out = _configure_case_logging(spec)
    metrics = MetricsLogger(namespace=spec["namespace"], service=spec.get("service"))
    for call in spec["calls"]:
        _call_metrics(metrics, call)
    return _captured_lines(out)


def _call_metrics(metrics: MetricsLogger, call: dict[str, Any]) -> None:
    dimensions = call.get("dimensions")
    match call["op"]:
        case "emit":
            metrics.emit(
                call["metrics"],
                unit=call.get("unit", "Count"),
                dimensions=dimensions,
                units=call.get("units"),
                rollup_dimension_sets=call.get("rollup_dimension_sets"),
                dimension_sets=call.get("dimension_sets"),
            )
        case "count":
            metrics.count(call["name"], call.get("value", 1), dimensions=dimensions)
        case "zero":
            metrics.zero(*call["names"], dimensions=dimensions)
        case "timing":
            metrics.timing(call["name"], call["milliseconds"], dimensions=dimensions)
        case "dependency_call":
            with metrics.dependency_call(call["dependency"], resource=call.get("resource")):
                if "error_type" in call:
                    raise _error_class(call["error_type"])()
        case op:
            raise ValueError(f"unknown metrics op: {op}")


def run_metrics(spec: dict[str, Any]) -> dict[str, Any]:
    """Raw EMF records from a sink, the drop warnings, and what the calls raised."""
    sink = io.StringIO()
    try:
        metrics = MetricsLogger(
            namespace=spec["namespace"],
            service=spec.get("service"),
            default_dimensions=spec.get("default_dimensions"),
            sink=sink,
            strict=spec.get("strict", False),
        )
    except (AuditryError, TypeError) as exc:
        return {"records": [], "raises": type(exc).__name__}

    result: dict[str, Any] = {"records": []}
    with structlog.testing.capture_logs() as captured:
        for call in spec["calls"]:
            start = sink.tell()
            try:
                _call_metrics(metrics, call)
            except Exception as exc:
                result["raises"] = type(exc).__name__
            for line in sink.getvalue()[start:].splitlines():
                record = _normalize_record(json.loads(line))
                if call["op"] == "dependency_call":
                    record = _normalize_duration(record)
                result["records"].append(record)
            if "raises" in result:
                break
    warnings = [
        {key: entry[key] for key in ("violation", "dimension", "metric_names")}
        for entry in captured
        if entry.get("event") == "metric dropped"
    ]
    if warnings:
        result["warnings"] = warnings
    return result


RUNNERS = {
    "redact": run_redact,
    "strict": run_strict,
    "log": run_log,
    "metric_line": run_metric_line,
    "metrics": run_metrics,
}


def run_case(case: dict[str, Any]) -> Any:
    _reset_process_state()
    try:
        return RUNNERS[case["kind"]](case["input"])
    finally:
        _reset_process_state()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def regenerate() -> None:
    """Rewrites the contract and every case's expected output from the current code."""
    _dump(CONTRACT_PATH, build_contract())
    vectors = _load(VECTORS_PATH)
    for case in vectors["cases"]:
        case["expected"] = run_case(case)
    _dump(VECTORS_PATH, vectors)


CASES = _load(VECTORS_PATH)["cases"]


def test_contract_matches_the_code() -> None:
    assert _load(CONTRACT_PATH) == build_contract()


def test_case_names_are_unique() -> None:
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_vector(case: dict[str, Any]) -> None:
    assert run_case(case) == case["expected"]


if __name__ == "__main__":
    regenerate()
