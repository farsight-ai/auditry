"""Tests for auditry.metrics (EMF emitter with the dimension guard)."""

import io
import json
import logging

import pytest
import structlog
from asgi_correlation_id import correlation_id

from auditry.logging_config import _set_config_service, _set_strict, configure_logging
from auditry.metrics import ForbiddenDimensionError, MetricsLogger


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AUDITRY_STRICT", raising=False)
    correlation_id.set(None)
    _set_config_service(None)
    _set_strict(False)
    yield
    correlation_id.set(None)
    _set_config_service(None)
    _set_strict(False)
    structlog.reset_defaults()
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)


def make_logger(**kwargs):
    sink = io.StringIO()
    logger = MetricsLogger(namespace="Test/NS", service="test-svc", sink=sink, **kwargs)
    return logger, sink


def records(sink):
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def stream_records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]


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

    def test_zero_with_no_names_emits_nothing(self):
        # An empty metrics list is never a useful record.
        logger, sink = make_logger()
        logger.zero()
        logger.zero(*[])
        assert sink.getvalue() == ""

    def test_timing_unit(self):
        logger, sink = make_logger()
        logger.timing("Latency", 12.5)
        (rec,) = records(sink)
        metrics = rec["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert {"Name": "Latency", "Unit": "Milliseconds"} in metrics


class TestPipelineRouting:
    """Metric records ride the same log pipeline as everything else, so they
    carry the root schema and the correlation ID; CloudWatch ignores the
    extra keys."""

    def test_record_carries_root_schema_and_correlation_id(self, capsys):
        configure_logging(service="from-configure", version="1.2.3", environment="test")
        _set_config_service("svc-cfg")
        correlation_id.set("req-42")
        MetricsLogger(namespace="Test/NS", service="svc-cfg").count("Requests")
        (rec,) = stream_records(capsys)
        # EMF payload intact ...
        assert rec["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Test/NS"
        assert rec["Requests"] == 1
        assert rec["Service"] == "svc-cfg"
        # ... plus the shared shape, which is what lets a metric be tied back
        # to the request that produced it.
        assert rec["correlation_id"] == "req-42"
        assert rec["service"] == "svc-cfg"
        assert rec["version"] == "1.2.3"
        assert rec["environment"] == "test"
        assert rec["level"] == "info"
        assert rec["message"] == "metric"
        assert "timestamp" in rec

    def test_root_log_level_cannot_drop_metrics(self, capsys):
        # A WARNING root level must not silently discard INFO metric lines.
        configure_logging(level="WARNING", service="svc")
        MetricsLogger(namespace="Test/NS", service="svc").count("Requests")
        (rec,) = stream_records(capsys)
        assert rec["Requests"] == 1

    def test_falls_back_to_raw_stdout_without_logging_setup(self, capsys):
        # EMF extraction must not depend on configure_logging() having run.
        structlog.reset_defaults()
        assert not structlog.is_configured()
        MetricsLogger(namespace="Test/NS", service="svc").count("Requests")
        (rec,) = stream_records(capsys)
        assert rec["Requests"] == 1
        assert "correlation_id" not in rec

    def test_sink_is_a_raw_seam(self, capsys):
        configure_logging(service="svc")
        logger, sink = make_logger()
        logger.count("Requests")
        assert capsys.readouterr().out == ""
        (rec,) = records(sink)
        assert "timestamp" not in rec


class TestDimensionGuard:
    """PII/user content can never become a metric dimension — and a bad
    dimension can never break the code path being measured."""

    @pytest.mark.parametrize(
        "bad_key",
        ["userId", "user_id", "USER-ID", "email", "userEmail", "fileName",
         "file_name", "documentTitle", "prompt", "objectKey", "full_name"],
    )
    def test_forbidden_dimension_drops_record(self, bad_key):
        logger, sink = make_logger()
        logger.count("X", dimensions={bad_key: "value"})  # does not raise
        assert sink.getvalue() == ""  # nothing emitted

    @pytest.mark.parametrize("bad_key", ["userId", "prompt"])
    def test_forbidden_dimension_raises_in_strict_mode(self, bad_key):
        logger, sink = make_logger(strict=True)
        with pytest.raises(ForbiddenDimensionError):
            logger.count("X", dimensions={bad_key: "value"})
        assert sink.getvalue() == ""

    def test_forbidden_default_dimension_always_raises_at_construction(self):
        # Construction is startup, not the hot path: fail fast regardless.
        with pytest.raises(ForbiddenDimensionError):
            MetricsLogger(namespace="T", default_dimensions={"userId": "u1"}, sink=io.StringIO())

    def test_drop_warns_once_per_offending_name(self, capsys):
        configure_logging(service="svc")
        logger, sink = make_logger()
        for _ in range(5):
            logger.count("X", dimensions={"userId": "u1"})
        logger.count("X", dimensions={"prompt": "p"})
        warnings = [r for r in stream_records(capsys) if r.get("metric_dropped")]
        assert [w["dimension"] for w in warnings] == ["userId", "prompt"]
        assert warnings[0]["violation"] == "ForbiddenDimensionError"
        assert warnings[0]["metric_names"] == ["X"]
        assert warnings[0]["level"] == "warning"
        assert sink.getvalue() == ""

    def test_org_id_allowed(self):
        # Opaque tenant/org IDs are allowed.
        logger, sink = make_logger()
        logger.count("X", dimensions={"OrgId": "org_123"})
        assert records(sink)[0]["OrgId"] == "org_123"

    def test_too_many_dimensions_drops(self):
        logger, sink = make_logger()
        logger.count("X", dimensions={f"D{i}": "v" for i in range(9)})
        assert sink.getvalue() == ""

    def test_too_many_dimensions_raises_in_strict_mode(self):
        logger, _ = make_logger(strict=True)
        with pytest.raises(ValueError, match="exceeds"):
            logger.count("X", dimensions={f"D{i}": "v" for i in range(9)})


class TestDimensionValues:
    def test_scalars_are_coerced_to_strings(self):
        # A retry counter is an ordinary dimension value, not a privacy
        # violation.
        logger, sink = make_logger()
        logger.count("Retries", dimensions={"Attempt": 3, "Ratio": 0.5, "Cached": True})
        (rec,) = records(sink)
        assert rec["Attempt"] == "3"
        assert rec["Ratio"] == "0.5"
        assert rec["Cached"] == "True"

    def test_scalars_coerced_in_default_dimensions_too(self):
        m = MetricsLogger(namespace="T", default_dimensions={"Shard": 7}, sink=io.StringIO())
        assert m.default_dimensions["Shard"] == "7"

    def test_non_scalar_is_a_type_error_not_a_policy_violation(self):
        logger, sink = make_logger(strict=True)
        with pytest.raises(TypeError):
            logger.emit({"X": 1}, dimensions={"Dependency": {"nested": 1}})
        with pytest.raises(TypeError):
            logger.emit({"X": 1}, dimensions={"Dependency": None})
        assert sink.getvalue() == ""

    def test_values_must_be_bounded_identifiers(self):
        """Value-side guard: empty, over-long, or multi-line values indicate
        user content in a dimension. Pattern-matching values would misfire
        ('email-service' is a fine Dependency), so the guard is structural."""
        logger, sink = make_logger(strict=True)
        for bad in ["", "a" * 129, "line1\nline2"]:
            with pytest.raises(ForbiddenDimensionError):
                logger.emit({"X": 1}, dimensions={"Dependency": bad})
        # Legitimate identifiers that merely CONTAIN a forbidden word pass.
        logger.emit({"X": 1}, dimensions={"Dependency": "email-service"})
        assert records(sink)[0]["Dependency"] == "email-service"


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

    def test_bad_default_dimension_never_breaks_the_measured_call(self):
        # The instrumented call runs and its result is returned even when the
        # metric itself is dropped for a validation problem.
        logger, sink = make_logger()
        logger.default_dimensions["userId"] = "u1"  # bypass construction check
        ran = []
        with logger.dependency_call("redis"):
            ran.append(True)
        assert ran == [True]
        assert sink.getvalue() == ""


def test_dependency_call_error_rolls_up_to_dependency_set():
    """Error emission records under BOTH the full set (with ErrorType) and
    the coarser per-dependency set, so alarms need not enumerate error types."""
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
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    with m.dependency_call("db"):
        pass
    record = json.loads(sink.getvalue().strip().splitlines()[-1])
    dim_sets = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert sorted(map(sorted, dim_sets)) == [["Dependency", "Service"]]


def test_emit_drops_when_rollup_dimensions_not_in_record():
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    m.emit({"X": 1}, dimensions={"A": "a"}, rollup_dimension_sets=[["Nope"]])
    assert sink.getvalue() == ""
    with pytest.raises(ValueError, match="rollup"):
        MetricsLogger(namespace="Test/NS", service="svc", sink=sink, strict=True).emit(
            {"X": 1}, dimensions={"A": "a"}, rollup_dimension_sets=[["Nope"]]
        )


def test_emit_validates_merged_dimension_set():
    """Defaults + call-specific dimensions are validated together — the
    cardinality cap can't be sidestepped by splitting them."""
    m = MetricsLogger(
        namespace="Test/NS",
        service="svc",
        default_dimensions={f"D{i}": "x" for i in range(7)},  # 8 with Service
        sink=io.StringIO(),
        strict=True,
    )
    with pytest.raises(ValueError, match="9 dimensions"):
        m.count("C", 1, dimensions={"Extra": "x"})


def test_emit_rejects_reserved_and_colliding_names():
    """The EMF record is flat — dimension/metric names must not collide with
    each other or with the reserved _aws metadata key."""
    sink = io.StringIO()
    m = MetricsLogger(namespace="Test/NS", service="svc", sink=sink, strict=True)
    with pytest.raises(ValueError, match="collide"):
        m.emit({"_aws": 1})
    with pytest.raises(ValueError, match="collide"):
        m.emit({"Service": 1})  # metric name overwrites the Service dimension
    with pytest.raises(ValueError, match="collide"):
        m.emit({"X": 1}, dimensions={"_aws": "v"})
    # ... and in the default mode the same records are simply dropped.
    lenient = MetricsLogger(namespace="Test/NS", service="svc", sink=sink)
    lenient.emit({"_aws": 1})
    lenient.emit({"Service": 1})
    assert sink.getvalue() == ""


class TestStrictFollowsProcessPolicy:
    """MetricsLogger(strict=None) follows the policy configure_logging()
    resolved from the environment; an explicit instance value overrides it."""

    def test_default_follows_process_policy(self):
        logger, sink = make_logger()
        _set_strict(True)
        with pytest.raises(ForbiddenDimensionError):
            logger.count("X", dimensions={"userId": "u"})
        _set_strict(False)
        logger.count("X", dimensions={"userId": "u"})  # dropped, no raise
        assert sink.getvalue() == ""

    def test_instance_override_beats_policy(self):
        _set_strict(True)
        lenient, sink = make_logger(strict=False)
        lenient.count("X", dimensions={"userId": "u"})  # no raise
        _set_strict(False)
        strict_logger, _ = make_logger(strict=True)
        with pytest.raises(ForbiddenDimensionError):
            strict_logger.count("X", dimensions={"userId": "u"})
        assert sink.getvalue() == ""

    def test_configure_logging_environment_decides(self, capsys):
        # The same call site raises in dev and warns in prod — nothing to
        # wire per service.
        configure_logging(service="svc", environment="dev")
        with pytest.raises(ForbiddenDimensionError):
            MetricsLogger(namespace="T", service="svc").count("X", dimensions={"userId": "u"})
        configure_logging(service="svc", environment="prod")
        MetricsLogger(namespace="T", service="svc").count("X", dimensions={"userId": "u"})
        assert any(r.get("metric_dropped") for r in stream_records(capsys))

    def test_policy_is_read_at_emit_time(self):
        # A module-level MetricsLogger built before configure_logging() still
        # follows the policy configure_logging() resolves later.
        logger, sink = make_logger()
        configure_logging(service="svc", environment="sandbox")
        with pytest.raises(ForbiddenDimensionError):
            logger.count("X", dimensions={"prompt": "p"})
