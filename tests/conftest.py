"""Shared fixtures. None are autouse: a module opts in by naming them."""

import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest
import structlog
from asgi_correlation_id import correlation_id

from auditry.logging_config import _set_config_service, _set_strict, set_trace_handler
from auditry.propagation import _set_correlation_header
from auditry.span_scrubbing import set_span_scrub_policy
from auditry.tracing import _reset_warnings


@dataclass
class OtelHarness:
    """One process-wide TracerProvider that records every finished span
    twice: as the SDK produced it (``raw``) and after auditry's scrubbing
    exporter (``scrubbed``)."""

    provider: Any
    raw: Any
    scrubbed: Any

    @property
    def tracer(self) -> Any:
        return self.provider.get_tracer("auditry-tests")

    def raw_spans(self) -> list[Any]:
        return list(self.raw.get_finished_spans())

    def scrubbed_spans(self) -> list[Any]:
        return list(self.scrubbed.get_finished_spans())

    def raw_server_spans(self) -> list[Any]:
        return [span for span in self.raw_spans() if span.kind.name == "SERVER"]

    def scrubbed_server_spans(self) -> list[Any]:
        return [span for span in self.scrubbed_spans() if span.kind.name == "SERVER"]

    def clear(self) -> None:
        self.raw.clear()
        self.scrubbed.clear()


@pytest.fixture(scope="session")
def _otel_session() -> OtelHarness:
    # Session-scoped and optional: modules that never name it run without
    # the otel extra, so its modules are skipped here, not imported at the top.
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    sdk_export = pytest.importorskip("opentelemetry.sdk.trace.export")
    in_memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    trace = pytest.importorskip("opentelemetry.trace")
    auditry_otel = pytest.importorskip("auditry.otel")

    raw = in_memory.InMemorySpanExporter()
    scrubbed = in_memory.InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(sdk_export.SimpleSpanProcessor(raw))
    provider.add_span_processor(
        sdk_export.SimpleSpanProcessor(auditry_otel.ScrubbingSpanExporter(scrubbed))
    )
    # Takes effect once per process; instrumentors that accept
    # tracer_provider= are also given it explicitly.
    trace.set_tracer_provider(provider)
    return OtelHarness(provider=provider, raw=raw, scrubbed=scrubbed)


@pytest.fixture
def otel(_otel_session: OtelHarness) -> Any:
    """The shared harness with both exporters empty and the default span
    scrub policy."""
    _otel_session.clear()
    set_span_scrub_policy(None)
    yield _otel_session
    _otel_session.clear()
    set_span_scrub_policy(None)


@pytest.fixture
def clean_logging(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Production policy, nothing bound, nothing configured, before and after."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AUDITRY_STRICT", raising=False)

    def reset() -> None:
        correlation_id.set(None)
        structlog.contextvars.clear_contextvars()
        set_trace_handler(None)
        _set_config_service(None)
        _set_correlation_header(None)
        _set_strict(False)
        _reset_warnings()

    reset()
    yield
    reset()
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


@pytest.fixture
def log_lines(capsys: pytest.CaptureFixture[str]) -> Any:
    """Call it to read every JSON log line written to stdout since the last read."""

    def read() -> list[dict[str, Any]]:
        out = capsys.readouterr().out
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    return read
