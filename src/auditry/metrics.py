"""
CloudWatch metrics via Embedded Metric Format (EMF) — stdlib only.

EMF writes metrics as structured JSON log lines that CloudWatch extracts
server-side, so there is no SDK dependency, no network call on the hot path,
and the awslogs driver ships them with the ordinary log stream.

Conventions enforced here:

- ``dependency_call()`` times every dependency and counts success/error
  per resource.
- a counter per error reason (exception class name), and ``zero()`` for
  emitting zero counts on conditions you alarm on (keeps "no data" alarms
  meaningful).
- metric dimensions must never contain PII or user content: dimension names
  are validated against a forbidden list (userId, email, file names,
  prompts, ...). Metrics platforms are unencrypted, widely readable, and
  unerasable. Aggregate by opaque tenant/org ID, never by a user-entered
  value.
- instrumentation never breaks the code it instruments. On the emit path a
  violation drops the record and logs one warning per offending name; it
  does not raise into the request handler. In **strict mode** the same
  violations raise — resolved process-wide by ``configure_logging()`` from
  the environment (strict only in a known non-production environment), or
  forced per instance with ``strict=``. ``default_dimensions`` are validated
  at construction (startup, not the hot path) and always raise.
- metric records ride the same log pipeline as everything else: when
  ``configure_logging()`` has run, each record carries the root schema
  (``timestamp``, ``service``, ``version``, ``environment``,
  ``correlation_id``) alongside the EMF payload, so a metric can be tied
  back to the request that produced it. CloudWatch ignores the extra keys.

Usage:

```python
from auditry.metrics import MetricsLogger

metrics = MetricsLogger(namespace="MyCompany/MyService", service="my-service")

with metrics.dependency_call("redis"):
    value = await cache.get(key)

metrics.count("SchemaValidationFailed", 0)   # zero count keeps alarms alive
```
"""

import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Type

import structlog

from .logging_config import is_strict

__all__ = ["MetricsLogger", "ForbiddenDimensionError"]

# Dimension names that indicate PII or user content. Substring match,
# case/format-insensitive ("userId", "user_id", "USER-ID" all match).
FORBIDDEN_DIMENSION_PATTERNS = (
    "userid",
    "username",
    "useremail",
    "email",
    "filename",
    "filepath",
    "objectkey",
    "document",
    "prompt",
    "completion",
    "message",
    "title",
    "query",
    "ticker",
    "ssn",
    "phone",
    "address",
    "firstname",
    "lastname",
    "fullname",
)

_MAX_DIMENSIONS = 8  # CloudWatch EMF hard limit is 30; keep cardinality sane.

# Dimension VALUES must be short, single-line identifiers. Pattern-matching
# values the way we match keys would misfire constantly ("email-service" is a
# perfectly good Dependency value), so the value guard is structural: user
# content is free-form and long; identifiers are short and single-line.
_MAX_DIMENSION_VALUE_LEN = 128

# Metric records and drop warnings go through this logger. Its level is
# pinned to INFO so a WARNING root level cannot silently discard metrics.
_LOGGER_NAME = "auditry.metrics"


class ForbiddenDimensionError(ValueError):
    """A metric dimension would carry PII or user content (policy violation)."""


# A validation problem: the exception class it maps to in strict mode, the
# offending name (dimension or metric), and the human-readable reason.
_Problem = Tuple[Type[Exception], str, str]


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _coerce_dimension_values(dimensions: Dict[str, Any]) -> Dict[str, Any]:
    """Scalars are ordinary dimension values — ``{"Attempt": 3}`` is a retry
    counter, not a privacy violation. Coerce numbers and booleans to str;
    leave everything else for validation to reject."""
    return {
        key: (str(value) if isinstance(value, (int, float, bool)) else value)
        for key, value in dimensions.items()
    }


def _dimension_problems(dimensions: Dict[str, Any]) -> List[_Problem]:
    problems: List[_Problem] = []
    for key, value in dimensions.items():
        norm = _normalize(key)
        for pattern in FORBIDDEN_DIMENSION_PATTERNS:
            if pattern in norm:
                problems.append((
                    ForbiddenDimensionError,
                    key,
                    f"dimension name '{key}' matches forbidden pattern '{pattern}' — "
                    "metric dimensions must never carry PII or user content (they "
                    "are unencrypted, widely readable, and unerasable); aggregate "
                    "by an opaque tenant/org ID instead",
                ))
                break
        # Value side. Type errors are misuse, not policy: they map to
        # TypeError so "you passed a dict" never reads as "you leaked PII".
        if not isinstance(value, str):
            problems.append((
                TypeError,
                key,
                f"dimension '{key}' value must be a string or number, got "
                f"{type(value).__name__}",
            ))
            continue
        # Structural guard on the value: bounded, single-line strings only.
        # This catches free-form content (a prompt, a document, an error
        # message) being passed where an identifier belongs.
        if not value:
            problems.append((
                ForbiddenDimensionError, key, f"dimension '{key}' value is empty"
            ))
        elif len(value) > _MAX_DIMENSION_VALUE_LEN or "\n" in value:
            problems.append((
                ForbiddenDimensionError,
                key,
                f"dimension '{key}' value is "
                f"{'multi-line' if chr(10) in value else 'too long'} (max "
                f"{_MAX_DIMENSION_VALUE_LEN} chars, single-line) — long or "
                "multi-line values indicate user content in a dimension",
            ))
    if len(dimensions) > _MAX_DIMENSIONS:
        problems.append((
            ValueError,
            "*",
            f"{len(dimensions)} dimensions exceeds the sane-cardinality cap of "
            f"{_MAX_DIMENSIONS}; every dimension set is a distinct metric",
        ))
    return problems


def _record_problems(
    dims: Dict[str, Any],
    metrics: Dict[str, float],
    rollup_dimension_sets: Optional[List[List[str]]],
) -> List[_Problem]:
    """Problems with the record as a whole, beyond the dimensions."""
    problems: List[_Problem] = []
    # The EMF record is flat: _aws metadata, dimension values, and metric
    # values share one JSON object. Names that collide would destroy the
    # metadata or silently overwrite a value.
    reserved = {"_aws"} & (set(dims) | set(metrics))
    colliding = set(dims) & set(metrics)
    if reserved or colliding:
        problems.append((
            ValueError,
            ",".join(sorted(reserved | colliding)),
            f"metric/dimension names collide in the flattened EMF record "
            f"(reserved: {sorted(reserved)}, overlapping: {sorted(colliding)}) — "
            "metric and dimension names must be distinct and must not use "
            "reserved EMF fields",
        ))
    for rollup in rollup_dimension_sets or []:
        missing = [k for k in rollup if k not in dims]
        if missing:
            problems.append((
                ValueError,
                ",".join(missing),
                f"rollup dimension(s) {missing} not present in the record",
            ))
    return problems


def _raise_first(problems: List[_Problem]) -> None:
    exc_type, _, reason = problems[0]
    raise exc_type(reason)


class MetricsLogger:
    """
    Minimal EMF emitter with dimension validation.

    Args:
        namespace: CloudWatch namespace (e.g. ``MyCompany/MyService``).
        service: Service name added as a default dimension.
        default_dimensions: Extra default dimensions. Validated at
            construction and always raise on a violation.
        sink: Writable used for output instead of the log pipeline. A test
            seam — pass a StringIO to capture raw EMF lines.
        strict: Raise on emit-path violations instead of dropping the record
            and warning. Leave unset (the default) to follow the process-wide
            policy that ``configure_logging()`` derives from the environment:
            strict in a known non-production environment, production-safe
            everywhere else. Pass True/False to override for this instance.
    """

    def __init__(
        self,
        namespace: str,
        service: Optional[str] = None,
        default_dimensions: Optional[Dict[str, Any]] = None,
        sink: Any = None,
        strict: Optional[bool] = None,
    ):
        self.namespace = namespace
        self._strict = strict
        self.default_dimensions: Dict[str, str] = {}
        if service:
            self.default_dimensions["Service"] = service
        if default_dimensions:
            self.default_dimensions.update(_coerce_dimension_values(default_dimensions))
        # Construction is startup, not the hot path: always fail fast here.
        problems = _dimension_problems(self.default_dimensions)
        if problems:
            _raise_first(problems)
        self._sink = sink
        self._warned: Set[Tuple[str, str]] = set()
        logging.getLogger(_LOGGER_NAME).setLevel(logging.INFO)

    @property
    def strict(self) -> bool:
        """Instance override if given, else the process-wide policy — read at
        emit time, so configure_logging() may run after construction."""
        return self._strict if self._strict is not None else is_strict()

    # -- core ---------------------------------------------------------------

    def emit(
        self,
        metrics: Dict[str, float],
        unit: str = "Count",
        dimensions: Optional[Dict[str, Any]] = None,
        units: Optional[Dict[str, str]] = None,
        rollup_dimension_sets: Optional[List[List[str]]] = None,
    ) -> None:
        """
        Emit one EMF record carrying one or more metric values.

        ``units`` overrides ``unit`` per metric name where they differ
        (e.g. ``{"Latency": "Milliseconds"}``).

        ``rollup_dimension_sets`` adds extra EMF dimension sets (each a list
        of dimension names, subset of the full set) so the same values are
        also recorded under coarser dimensions — e.g. record an error under
        ``[Dependency, ErrorType]`` AND ``[Dependency]`` so alarms can target
        the coarser series without enumerating error types. One record, no
        double counting per set.

        A record that fails validation is dropped with one warning per
        offending name (or raises, in strict mode); this never raises into the
        caller in production.
        """
        if not metrics:
            return
        dims = dict(self.default_dimensions)
        if dimensions:
            dims.update(_coerce_dimension_values(dimensions))
        # Validate the MERGED set — defaults plus call-specific — so the
        # cardinality cap can't be sidestepped by splitting dimensions
        # across the constructor and the call.
        problems = _dimension_problems(dims) + _record_problems(
            dims, metrics, rollup_dimension_sets
        )
        if problems:
            self._reject(problems, metrics)
            return

        dimension_sets: List[List[str]] = [list(dims.keys())] if dims else [[]]
        for rollup in rollup_dimension_sets or []:
            dimension_sets.append(list(rollup))

        record: Dict[str, Any] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": dimension_sets,
                        "Metrics": [
                            {"Name": name, "Unit": (units or {}).get(name, unit)}
                            for name in metrics
                        ],
                    }
                ],
            },
            **dims,
            **metrics,
        }
        self._write(record)

    def _reject(self, problems: List[_Problem], metrics: Dict[str, float]) -> None:
        if self.strict:
            _raise_first(problems)
        # Drop the record; warn once per (problem kind, name) per instance so
        # a hot path with a bad dimension does not turn into a log storm.
        for exc_type, name, reason in problems:
            marker = (exc_type.__name__, name)
            if marker in self._warned:
                continue
            self._warned.add(marker)
            structlog.get_logger(_LOGGER_NAME).warning(
                "metric dropped",
                metric_dropped=True,
                metric_names=sorted(metrics),
                dimension=name,
                reason=reason,
                violation=exc_type.__name__,
            )

    def _write(self, record: Dict[str, Any]) -> None:
        if self._sink is not None:
            # Test seam: raw EMF line, nothing else.
            self._sink.write(json.dumps(record, default=str) + "\n")
            return
        if structlog.is_configured():
            # Through the shared pipeline: the record picks up the root
            # schema and the correlation ID, and is rendered once, as one
            # JSON line, by the same formatter as every other line.
            structlog.get_logger(_LOGGER_NAME).info("metric", **record)
            return
        # No logging configured (a script, a bare test): EMF extraction must
        # not depend on it, so write the raw line ourselves.
        sys.stdout.write(json.dumps(record, default=str) + "\n")

    # -- conveniences ---------------------------------------------------------

    def count(
        self, name: str, value: float = 1, dimensions: Optional[Dict[str, Any]] = None
    ) -> None:
        """Counter. Use ``value=0`` to keep alarmable metrics alive."""
        self.emit({name: value}, unit="Count", dimensions=dimensions)

    def zero(self, *names: str, dimensions: Optional[Dict[str, Any]] = None) -> None:
        """Emit zero counts for rare conditions you alarm on — monitoring
        systems forget metrics that go silent, and zero counts enable
        "no data" alarms. No names, no record."""
        if not names:
            return
        self.emit({name: 0 for name in names}, unit="Count", dimensions=dimensions)

    def timing(
        self, name: str, milliseconds: float, dimensions: Optional[Dict[str, Any]] = None
    ) -> None:
        """Latency value in milliseconds."""
        self.emit({name: milliseconds}, unit="Milliseconds", dimensions=dimensions)

    @contextmanager
    def dependency_call(
        self,
        dependency: str,
        resource: Optional[str] = None,
    ) -> Iterator[None]:
        """
        Time a dependency call and count success/error, with the error
        reason (exception class name) as an ErrorType dimension.

        ```python
        with metrics.dependency_call("dynamodb", resource="jobs-table"):
            table.get_item(...)
        ```

        Emits ``Latency`` (ms), ``Success`` and ``Error`` counts under the
        ``Dependency`` (+ optional ``Resource``) dimension — success emits
        ``Error: 0`` and vice versa, so no-data alarms work.
        """
        dims: Dict[str, str] = {"Dependency": dependency}
        if resource:
            dims["Resource"] = resource
        # The coarse per-dependency set — no Resource, no ErrorType — that
        # alarms and availability math target without enumerating either.
        # Both outcomes must record here: errors so the alarm sees them,
        # successes so Error: 0 keeps the series alive between failures.
        dependency_set = [
            k for k in {**self.default_dimensions, "Dependency": dependency}
        ]
        rollups = [dependency_set] if resource else None
        start = time.perf_counter()
        try:
            yield
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            full_dims = {**dims, "ErrorType": type(exc).__name__}
            self.emit(
                {"Latency": elapsed_ms, "Success": 0, "Error": 1},
                units={"Latency": "Milliseconds"},
                unit="Count",
                dimensions=full_dims,
                # The error path always rolls up: its full set carries
                # ErrorType (and Resource, when given), never the coarse set.
                rollup_dimension_sets=[dependency_set],
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.emit(
                {"Latency": elapsed_ms, "Success": 1, "Error": 0},
                units={"Latency": "Milliseconds"},
                unit="Count",
                dimensions=dims,
                # Without a resource, dims IS the coarse set — a rollup
                # would duplicate it and double-count within the record.
                rollup_dimension_sets=rollups,
            )
