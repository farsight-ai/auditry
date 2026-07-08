"""Tests for auditry.metrics (EMF emitter with the dimension guard)."""

import io
import json

import pytest

from auditry.metrics import ForbiddenDimensionError, MetricsLogger


def make_logger(**kwargs):
    sink = io.StringIO()
    logger = MetricsLogger(namespace="Test/NS", service="test-svc", sink=sink, **kwargs)
    return logger, sink


def records(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


class TestEmit:
    def test_emf_structure(self):
        logger, sink = make_logger()
        logger.count("Requests")
        (rec,) = records(sink)
        assert rec["Requests"] == 1
        assert rec["Service"] == "test-svc"
        cw = rec["_aws"]["CloudWatchMetrics"][0]
        assert cw["Namespace"] == "Test/NS"
        assert {"Name": "Requests", "Unit": "Count"} in cw["Metrics"]
        assert "Service" in cw["Dimensions"][0]

    def test_single_line_output(self):
        logger, sink = make_logger()
        logger.count("A")
        logger.count("B")
        assert len(sink.getvalue().splitlines()) == 2

    def test_zero_counts(self):
        logger, sink = make_logger()
        logger.zero("RareError", "OtherRareError")
        (rec,) = records(sink)
        assert rec["RareError"] == 0
        assert rec["OtherRareError"] == 0

    def test_timing_unit(self):
        logger, sink = make_logger()
        logger.timing("Latency", 12.5)
        (rec,) = records(sink)
        metrics = rec["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert {"Name": "Latency", "Unit": "Milliseconds"} in metrics


class TestDimensionGuard:
    """PII/user content can never become a metric dimension."""

    @pytest.mark.parametrize(
        "bad_key",
        ["userId", "user_id", "USER-ID", "email", "userEmail", "fileName",
         "file_name", "documentTitle", "prompt", "objectKey", "full_name"],
    )
    def test_forbidden_dimension_raises(self, bad_key):
        logger, sink = make_logger()
        with pytest.raises(ForbiddenDimensionError):
            logger.count("X", dimensions={bad_key: "value"})
        assert sink.getvalue() == ""  # nothing emitted

    def test_forbidden_default_dimension_raises_at_construction(self):
        with pytest.raises(ForbiddenDimensionError):
            MetricsLogger(namespace="T", default_dimensions={"userId": "u1"}, sink=io.StringIO())

    def test_org_id_allowed(self):
        # Opaque tenant/org IDs are allowed.
        logger, sink = make_logger()
        logger.count("X", dimensions={"OrgId": "org_123"})
        assert records(sink)[0]["OrgId"] == "org_123"

    def test_too_many_dimensions(self):
        logger, _ = make_logger()
        with pytest.raises(ValueError):
            logger.count("X", dimensions={f"D{i}": "v" for i in range(9)})


class TestDependencyCall:
    """Per-dependency latency + success/error with zero counts."""

    def test_success_emits_latency_and_zero_error(self):
        logger, sink = make_logger()
        with logger.dependency_call("redis", resource="job-cache"):
            pass
        (rec,) = records(sink)
        assert rec["Success"] == 1
        assert rec["Error"] == 0  # zero count so no-data alarms work
        assert rec["Dependency"] == "redis"
        assert rec["Resource"] == "job-cache"
        assert rec["Latency"] >= 0

    def test_error_emits_error_type_and_reraises(self):
        logger, sink = make_logger()
        with pytest.raises(KeyError):
            with logger.dependency_call("bedrock"):
                raise KeyError("boom")
        (rec,) = records(sink)
        assert rec["Error"] == 1
        assert rec["Success"] == 0
        assert rec["ErrorType"] == "KeyError"  # error reason counter
