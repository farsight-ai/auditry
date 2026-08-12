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
  prompts, ...) and a violation raises immediately rather than emitting —
  metrics platforms are unencrypted, widely readable, and unerasable.
  Aggregate by opaque tenant/org ID, never by a user-entered value.

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
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

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


class ForbiddenDimensionError(ValueError):
    """Raised when a metric dimension would carry PII or user content."""


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _validate_dimensions(dimensions: Dict[str, str]) -> None:
    for key, value in dimensions.items():
        norm = _normalize(key)
        for pattern in FORBIDDEN_DIMENSION_PATTERNS:
            if pattern in norm:
                raise ForbiddenDimensionError(
                    f"Metric dimension '{key}' matches forbidden pattern "
                    f"'{pattern}' — metric dimensions must never carry PII or "
                    "user content (they are unencrypted, widely readable, and "
                    "unerasable). Aggregate by an opaque tenant/org ID instead."
                )
        # Structural guard on the value side: bounded, single-line strings
        # only. This catches free-form content (a prompt, a document, an
        # error message) being passed where an identifier belongs.
        if not isinstance(value, str) or not value:
            raise ForbiddenDimensionError(
                f"Metric dimension '{key}' must be a non-empty string, "
                f"got {type(value).__name__} — dimension values are "
                "identifiers, not data."
            )
        if len(value) > _MAX_DIMENSION_VALUE_LEN or "\n" in value:
            raise ForbiddenDimensionError(
                f"Metric dimension '{key}' value is "
                f"{'multi-line' if chr(10) in value else 'too long'} "
                f"(max {_MAX_DIMENSION_VALUE_LEN} chars, single-line) — "
                "long or multi-line values indicate user content in a "
                "dimension. Use a short, bounded identifier."
            )
    if len(dimensions) > _MAX_DIMENSIONS:
        raise ValueError(
            f"{len(dimensions)} dimensions exceeds the sane-cardinality cap "
            f"of {_MAX_DIMENSIONS}; every dimension set is a distinct metric."
        )


class MetricsLogger:
    """
    Minimal EMF emitter with dimension validation.

    Args:
        namespace: CloudWatch namespace (e.g. ``MyCompany/MyService``).
        service: Service name added as a default dimension.
        default_dimensions: Extra default dimensions (validated).
        sink: Writable used for output; defaults to stdout. Tests can pass
            a StringIO.
    """

    def __init__(
        self,
        namespace: str,
        service: Optional[str] = None,
        default_dimensions: Optional[Dict[str, str]] = None,
        sink: Any = None,
    ):
        self.namespace = namespace
        self.default_dimensions: Dict[str, str] = {}
        if service:
            self.default_dimensions["Service"] = service
        if default_dimensions:
            self.default_dimensions.update(default_dimensions)
        _validate_dimensions(self.default_dimensions)
        self._sink = sink if sink is not None else sys.stdout

    # -- core ---------------------------------------------------------------

    def emit(
        self,
        metrics: Dict[str, float],
        unit: str = "Count",
        dimensions: Optional[Dict[str, str]] = None,
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
        """
        dims = dict(self.default_dimensions)
        if dimensions:
            dims.update(dimensions)
        # Validate the MERGED set — defaults plus call-specific — so the
        # cardinality cap can't be sidestepped by splitting dimensions
        # across the constructor and the call.
        _validate_dimensions(dims)

        # The EMF record is flat: _aws metadata, dimension values, and metric
        # values share one JSON object. Reject names that would collide —
        # a dimension named "_aws" would destroy the metadata; a metric
        # sharing a dimension's name would silently overwrite its value.
        reserved = {"_aws"} & (set(dims) | set(metrics))
        colliding = set(dims) & set(metrics)
        if reserved or colliding:
            raise ValueError(
                f"metric/dimension names collide in the flattened EMF record "
                f"(reserved: {sorted(reserved)}, overlapping: {sorted(colliding)}) — "
                "metric and dimension names must be distinct and must not use "
                "reserved EMF fields."
            )

        dimension_sets: List[List[str]] = [list(dims.keys())] if dims else [[]]
        for rollup in rollup_dimension_sets or []:
            missing = [k for k in rollup if k not in dims]
            if missing:
                raise ValueError(
                    f"rollup dimension(s) {missing} not present in the record"
                )
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
        # Single-line JSON on the standard stream: the log driver ships it,
        # and CloudWatch extracts the metric values at ingestion.
        self._sink.write(json.dumps(record, default=str) + "\n")

    # -- conveniences ---------------------------------------------------------

    def count(
        self, name: str, value: float = 1, dimensions: Optional[Dict[str, str]] = None
    ) -> None:
        """Counter. Use ``value=0`` to keep alarmable metrics alive."""
        self.emit({name: value}, unit="Count", dimensions=dimensions)

    def zero(self, *names: str, dimensions: Optional[Dict[str, str]] = None) -> None:
        """Emit zero counts for rare conditions you alarm on — monitoring
        systems forget metrics that go silent, and zero counts enable
        "no data" alarms."""
        self.emit({name: 0 for name in names}, unit="Count", dimensions=dimensions)

    def timing(
        self, name: str, milliseconds: float, dimensions: Optional[Dict[str, str]] = None
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
                # Also record under the set WITHOUT ErrorType, so alarms and
                # availability math can target the per-dependency series
                # without enumerating error types.
                rollup_dimension_sets=[
                    [k for k in {**self.default_dimensions, **dims}]
                ],
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.emit(
                {"Latency": elapsed_ms, "Success": 1, "Error": 0},
                units={"Latency": "Milliseconds"},
                unit="Count",
                dimensions=dims,
            )
