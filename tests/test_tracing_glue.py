"""Tests for auditry.tracing and the trace fields it adds to logs and metrics."""

import io
import logging
import re

import pytest

from auditry import MetricsLogger, configure_logging, get_logger
from auditry.errors import MetricRecordError
from auditry.tracing import (
    TRACE_LOG_FIELDS,
    attach_context,
    context_from_carrier,
    current_trace_fields,
    detach_context,
    inject_trace_context,
    require_otel,
    set_span_correlation_id,
)

pytest.importorskip("opentelemetry.sdk")
trace = pytest.importorskip("opentelemetry.trace")

pytestmark = pytest.mark.usefixtures("clean_logging")

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def span_ids(span):
    context = span.get_span_context()
    return trace.format_trace_id(context.trace_id), trace.format_span_id(context.span_id)


class TestLogFields:
    def test_inside_a_span_the_line_carries_the_span_ids(self, otel, log_lines):
        configure_logging(service="svc")
        with otel.tracer.start_as_current_span("work") as span:
            get_logger("t").info("inside")
        (line,) = log_lines()
        assert TRACE_ID.match(line["trace_id"])
        assert SPAN_ID.match(line["span_id"])
        assert (line["trace_id"], line["span_id"]) == span_ids(span)

    def test_outside_a_span_both_fields_are_absent(self, log_lines):
        configure_logging(service="svc")
        get_logger("t").info("outside")
        (line,) = log_lines()
        for field in TRACE_LOG_FIELDS:
            assert field not in line

    def test_explicit_fields_are_not_overwritten(self, otel, log_lines):
        configure_logging(service="svc")
        with otel.tracer.start_as_current_span("work"):
            get_logger("t").info("explicit", trace_id="mine")
        (line,) = log_lines()
        assert line["trace_id"] == "mine"
        assert SPAN_ID.match(line["span_id"])

    def test_foreign_stdlib_records_carry_them_too(self, otel, log_lines):
        configure_logging(service="svc")
        with otel.tracer.start_as_current_span("work") as span:
            logging.getLogger("vendor").warning("stdlib line")
        (line,) = log_lines()
        assert (line["trace_id"], line["span_id"]) == span_ids(span)


class TestProcessorFailure:
    @pytest.fixture
    def broken_trace_fields(self, monkeypatch):
        def explode():
            raise RuntimeError("detail that must not be logged")

        monkeypatch.setattr("auditry.logging_config.current_trace_fields", explode)

    @pytest.mark.usefixtures("broken_trace_fields")
    def test_production_skips_the_fields_and_warns_once(self, log_lines):
        configure_logging(service="svc", environment="prod")
        logger = get_logger("t")
        logger.info("first")
        logger.info("second")
        lines = log_lines()
        warnings = [line for line in lines if line["message"] == "tracing signal dropped"]
        assert len(warnings) == 1
        assert warnings[0]["operation"] == "trace_log_fields"
        assert warnings[0]["error_type"] == "RuntimeError"
        assert [line["message"] for line in lines if line not in warnings] == ["first", "second"]
        for line in lines:
            assert "trace_id" not in line
            assert "detail that must not be logged" not in str(line)

    @pytest.mark.usefixtures("broken_trace_fields")
    def test_strict_mode_raises(self):
        configure_logging(service="svc", strict=True)
        with pytest.raises(RuntimeError):
            get_logger("t").info("boom")


class TestMetrics:
    def test_emf_record_inside_a_span_carries_both(self, otel, log_lines):
        configure_logging(service="svc")
        with otel.tracer.start_as_current_span("work") as span:
            MetricsLogger(namespace="T", service="svc").count("Requests")
        (record,) = [line for line in log_lines() if "_aws" in line]
        assert (record["trace_id"], record["span_id"]) == span_ids(span)

    @pytest.mark.parametrize("name", TRACE_LOG_FIELDS)
    def test_reserved_metric_name_is_dropped_with_a_warning(self, name, log_lines):
        configure_logging(service="svc", environment="prod")
        MetricsLogger(namespace="T", service="svc").count(name)
        lines = log_lines()
        assert not any("_aws" in line for line in lines)
        assert any(line.get("metric_dropped") for line in lines)

    @pytest.mark.parametrize("name", TRACE_LOG_FIELDS)
    def test_reserved_metric_name_raises_in_strict_mode(self, name):
        logger = MetricsLogger(namespace="T", service="svc", sink=io.StringIO(), strict=True)
        with pytest.raises(MetricRecordError, match="reserved"):
            logger.count(name)


class TestGlue:
    def test_current_trace_fields_outside_a_span(self):
        assert current_trace_fields() == {}

    def test_inject_writes_traceparent_inside_a_span_only(self, otel):
        outside: dict[str, str] = {}
        inject_trace_context(outside)
        assert outside == {}
        with otel.tracer.start_as_current_span("work") as span:
            inside: dict[str, str] = {}
            inject_trace_context(inside)
        trace_id, span_id = span_ids(span)
        flags = span.get_span_context().trace_flags
        assert inside["traceparent"] == f"00-{trace_id}-{span_id}-{flags:02x}"

    def test_context_from_carrier_round_trip(self, otel):
        with otel.tracer.start_as_current_span("producer") as span:
            carrier: dict[str, str] = {}
            inject_trace_context(carrier)
        ctx = context_from_carrier(carrier)
        assert ctx is not None
        remote = trace.get_current_span(ctx).get_span_context()
        assert remote.trace_id == span.get_span_context().trace_id
        assert remote.is_remote

    @pytest.mark.parametrize(
        "carrier",
        [
            {},
            {"traceparent": "garbage"},
            {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
        ],
    )
    def test_context_from_carrier_without_a_valid_traceparent(self, carrier):
        assert context_from_carrier(carrier) is None

    def test_attach_and_detach_accept_none(self):
        assert attach_context(None) is None
        detach_context(None)

    def test_attach_then_detach_restores_the_previous_context(self, otel):
        with otel.tracer.start_as_current_span("producer"):
            carrier: dict[str, str] = {}
            inject_trace_context(carrier)
        token = attach_context(context_from_carrier(carrier))
        assert trace.get_current_span().get_span_context().is_valid
        detach_context(token)
        assert not trace.get_current_span().get_span_context().is_valid

    def test_set_span_correlation_id_on_a_recording_span(self, otel):
        with otel.tracer.start_as_current_span("work"):
            set_span_correlation_id("cid-1")
            set_span_correlation_id(None)
        (span,) = otel.raw_spans()
        assert span.attributes["correlation_id"] == "cid-1"

    def test_set_span_correlation_id_outside_a_span_is_a_no_op(self):
        set_span_correlation_id("cid-1")

    def test_require_otel_passes_when_installed(self):
        require_otel("feature", "opentelemetry.sdk")

    def test_require_otel_names_the_extra_for_a_missing_module(self):
        with pytest.raises(ImportError, match=r"feature requires the 'otel' extra"):
            require_otel("feature", "opentelemetry.not_a_real_module")


class TestGlueFailure:
    @pytest.fixture
    def broken_propagator(self, monkeypatch):
        class Broken:
            def inject(self, carrier):
                raise RuntimeError("detail that must not be logged")

        monkeypatch.setattr("auditry.tracing._propagator", Broken())

    @pytest.mark.usefixtures("broken_propagator")
    def test_production_drops_the_signal_and_warns_once(self, log_lines):
        configure_logging(service="svc", environment="prod")
        carrier: dict[str, str] = {}
        assert inject_trace_context(carrier) is None
        inject_trace_context(carrier)
        assert carrier == {}
        warnings = [line for line in log_lines() if line.get("operation")]
        assert [(w["operation"], w["error_type"]) for w in warnings] == [
            ("inject_trace_context", "RuntimeError")
        ]
        assert "detail that must not be logged" not in str(warnings)

    @pytest.mark.usefixtures("broken_propagator")
    def test_strict_mode_raises(self):
        configure_logging(service="svc", strict=True)
        with pytest.raises(RuntimeError):
            inject_trace_context({})
