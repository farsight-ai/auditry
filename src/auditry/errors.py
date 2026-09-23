"""Exception hierarchy. Catch ``AuditryError`` to handle anything auditry raises."""


class AuditryError(Exception):
    """Base class for every exception auditry raises."""


class ForbiddenDimensionError(AuditryError, ValueError):
    """A metric dimension would carry PII or user content (policy violation)."""


class DimensionLimitError(AuditryError, ValueError):
    """A metric record has more dimensions than the cardinality cap allows."""


class MetricRecordError(AuditryError, ValueError):
    """A metric record is malformed: reserved or colliding names, or a rollup
    naming a dimension the record does not carry."""
