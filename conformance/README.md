# Conformance

auditry's log and metric behavior recorded as data, so a port to another
language can prove it writes the same lines. The Python package is the
reference: both files are generated from its code, and
`tests/test_conformance.py` fails whenever either has drifted from it.

Neither file ships in the wheel.

## Contents

- [Files](#files)
- [Regenerating](#regenerating)
- [Cases](#cases)
- [Placeholders](#placeholders)
- [Dimension values](#dimension-values)
- [Using it from a port](#using-it-from-a-port)

## Files

| File | Holds |
| --- | --- |
| `contract.json` | The tables a port reads rather than restates: redaction patterns and marker, forbidden dimension patterns, limits, reserved record keys, non-production environment names, the `AUDITRY_*` environment variables |
| `vectors.json` | Cases: an input and the output auditry produces for it |

Names that appear only as literals in the code (`Service`, `Dependency`,
`Latency`, the level names, `message`, `metric dropped`, and the
`SERVICE_NAME`, `SERVICE_VERSION` and `ENVIRONMENT` variables) are not in
`contract.json`. The vectors pin them.

## Regenerating

After a change to the code, or a new case added to `vectors.json` with any
`expected` value:

```bash
.venv/bin/python -m tests.test_conformance
```

It rewrites `contract.json` and every case's `expected`. Review the diff: a
changed expectation is a change every port has to make.

## Cases

Every case has a `name`, a `kind`, an `input` and an `expected` output, and
optionally `requires`: what a port must be able to represent to run it (see
[Dimension values](#dimension-values)). A case starts with `SERVICE_NAME`,
`SERVICE_VERSION`, `ENVIRONMENT`, `AUDITRY_STRICT` and
`AUDITRY_FULL_TRACEBACKS` unset, nothing configured and nothing bound.

| Kind | Input | Expected |
| --- | --- | --- |
| `redact` | `data`, optional `additional_patterns` | the redacted copy of `data` |
| `strict` | optional `explicit` (the `strict=` argument), `environment_variable` (`AUDITRY_STRICT`), `environment` | whether strict mode is on |
| `log` | optional `configure` (`service`, `version`, `environment`), `environment_variables`, `level` (minimum, default `INFO`), `correlation_id`; `calls` | every line written, in order |
| `metric_line` | as `log`, plus `namespace` and `service` for a `MetricsLogger` writing through the configured pipeline; `calls` are metric calls | every line written |
| `metrics` | `namespace`, `service`, optional `default_dimensions`, `strict` (default `false`); `calls` | `records`: raw EMF records; `warnings`: drop warnings; `raises`: the exception class name, when a call or the constructor raised |

A `log` call is `level`, `message`, optional `fields`, and optional
`error_type`: the call is made with an exception of a class by that name, whose
message must not reach the line.

A metric call is an `op` and its arguments: `emit` (`metrics`, `unit`, `units`,
`dimensions`, `rollup_dimension_sets`, `dimension_sets`), `count` (`name`,
`value`, `dimensions`), `zero` (`names`, `dimensions`), `timing` (`name`,
`milliseconds`, `dimensions`), `dependency_call` (`dependency`, `resource`,
and `error_type` to make the wrapped call raise an exception of that class).
Calls stop at the first that raises. A warning carries only `violation`,
`dimension` and `metric_names`: its `reason` is prose for a human.

## Placeholders

Values that differ on every run are replaced before comparison:

| Placeholder | Replaces |
| --- | --- |
| `<timestamp>` | a line's `timestamp`, when it is ISO-8601 in UTC (`Z` or `+00:00`) |
| `<epoch-ms>` | `_aws.Timestamp`, when it is an integer |
| `<duration-ms>` | `Latency` in a `dependency_call` record, when it is a number of at least 0 |

A value that is malformed stays as written, so the comparison fails on it.

## Dimension values

A boolean or number dimension value becomes the string Python's `str()` writes
for it. A port writes the same string, so a value is the same CloudWatch series
whichever language recorded it.

| Value | Written |
| --- | --- |
| boolean | `True`, `False` |
| integer | its decimal digits: `3`, `-12` |
| float | the shortest digits that read back as the same float: `0.5`, `0.0001`. A float with no fractional part keeps `.0`: `3.0`. A magnitude below `1e-4`, or `1e16` and above, is in exponent form with a sign and at least two exponent digits: `1e-05`, `2.5e-07`, `1e+16` |
| infinity, NaN | `inf`, `-inf`, `nan` |

JSON does not carry every distinction Python makes. JavaScript has one number
type, so its `JSON.parse` reads `3.0` as `3` and a JavaScript port cannot tell
a float with no fractional part from an integer. A case that needs one lists
`integral-float` in `requires`. Infinity and NaN are not valid JSON, and
JavaScript cannot hold an integer beyond 2^53 - 1 exactly, so no case covers
them.

## Using it from a port

1. Vendor both files at a pinned commit of this repository, and check in CI
   that the vendored copies still equal the files at that commit.
2. Read the tables from `contract.json`.
3. Run every case in `vectors.json` through the port, apply the placeholders,
   and compare with `expected`. JSON objects compare without regard to key
   order. Skip a case whose `requires` names something the port cannot
   represent.
