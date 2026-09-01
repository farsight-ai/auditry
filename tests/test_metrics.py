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


def test_dependency_call_error_rolls_up_to_dependency_set():
    """Error emission records under BOTH the full set (with ErrorType) and
    the coarser per-dependency set, so alarms need not enumerate error types."""
    import io, json
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    try:
        with m.dependency_call("db"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    record = json.loads(sink.getvalue().strip().splitlines()[-1])
    dim_sets = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert sorted(map(sorted, dim_sets)) == sorted(
        map(sorted, [["Service", "Dependency", "ErrorType"], ["Service", "Dependency"]])
    )
    assert record["Error"] == 1
    assert record["ErrorType"] == "RuntimeError"


def test_dependency_call_resource_error_still_rolls_up_to_dependency_set():
    """With resource=, the coarse rollup must stay [Service, Dependency] —
    a Resource in the rollup would leave the documented per-dependency alarm
    series with no data."""
    import io, json
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    try:
        with m.dependency_call("dynamodb", resource="jobs-table"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    record = json.loads(sink.getvalue().strip().splitlines()[-1])
    dim_sets = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert sorted(map(sorted, dim_sets)) == sorted(
        map(
            sorted,
            [
                ["Service", "Dependency", "Resource", "ErrorType"],
                ["Service", "Dependency"],
            ],
        )
    )


def test_dependency_call_resource_success_rolls_up_error_zero():
    """Success with resource= also records on the coarse set, so Error: 0
    keeps the per-dependency alarm series alive between failures."""
    import io, json
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    with m.dependency_call("dynamodb", resource="jobs-table"):
        pass
    record = json.loads(sink.getvalue().strip().splitlines()[-1])
    dim_sets = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert sorted(map(sorted, dim_sets)) == sorted(
        map(sorted, [["Service", "Dependency", "Resource"], ["Service", "Dependency"]])
    )
    assert record["Success"] == 1
    assert record["Error"] == 0


def test_dependency_call_success_without_resource_has_single_set():
    """No resource, no rollup: dims already ARE the coarse set, and a
    duplicate dimension set would double-count within the record."""
    import io, json
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    with m.dependency_call("db"):
        pass
    record = json.loads(sink.getvalue().strip().splitlines()[-1])
    dim_sets = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert sorted(map(sorted, dim_sets)) == [["Dependency", "Service"]]


def test_emit_rejects_rollup_dimensions_not_in_record():
    import io, pytest
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=io.StringIO())
    with pytest.raises(ValueError):
        m.emit({"X": 1}, dimensions={"A": "a"}, rollup_dimension_sets=[["Nope"]])


def test_emit_validates_merged_dimension_set():
    """Defaults + call-specific dimensions are validated together — the
    cardinality cap can't be sidestepped by splitting them."""
    import io, pytest
    m = MetricsLogger(
        namespace="Test/NS",
        service="svc",
        default_dimensions={f"D{i}": "x" for i in range(7)},  # 8 with Service
        sink=io.StringIO(),
    )
    with pytest.raises(ValueError, match="9 dimensions"):
        m.count("C", 1, dimensions={"Extra": "x"})


def test_emit_rejects_reserved_and_colliding_names():
    """The EMF record is flat — dimension/metric names must not collide with
    each other or with the reserved _aws metadata key."""
    import io, pytest
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=io.StringIO())
    with pytest.raises(ValueError, match="collide"):
        m.emit({"_aws": 1})
    with pytest.raises(ValueError, match="collide"):
        m.emit({"Service": 1})  # metric name overwrites the Service dimension
    with pytest.raises(ValueError, match="collide"):
        m.emit({"X": 1}, dimensions={"_aws": "v"})


def test_dimension_values_must_be_bounded_identifiers():
    """Value-side guard: non-string, empty, over-long, or multi-line values
    indicate user content in a dimension and are rejected. Pattern-matching
    values would misfire ('email-service' is a fine Dependency), so the
    guard is structural."""
    import io, pytest
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=io.StringIO())
    for bad in [123, "", "a" * 129, "line1\nline2"]:
        with pytest.raises(ForbiddenDimensionError):
            m.emit({"X": 1}, dimensions={"Dependency": bad})
    # Legitimate identifiers that merely CONTAIN a forbidden word pass.
    m.emit({"X": 1}, dimensions={"Dependency": "email-service"})
