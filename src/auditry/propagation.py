"""
Correlation-ID propagation outside the ASGI request cycle.

The ASGI middlewares bind the correlation ID for HTTP requests. Everything
else — queue workers, schedulers, outbound calls, cross-queue hops — uses the
helpers here so the ID survives every boundary:

- HTTP: ``outbound_headers()`` on every outbound call.
- Workers (ARQ/Celery/plain consumers): the ``with_correlation`` decorator or
  the ``bound_correlation_id()`` context manager around each unit of work,
  BEFORE its first log line. ``bind_correlation_id()`` is the sticky form for
  a true edge (a script, a one-shot process).
- SQS/SNS: ``sqs_message_attributes()`` on send; ``bound_message_context()``
  around each received message, or ``bind_from_sqs_message()`` at a true edge.

With the ``otel`` extra installed, the HTTP and SQS/SNS helpers also carry the
W3C trace context (``traceparent``/``tracestate``), so a trace continues
across the same boundaries.

IDs are always random UUID4s — opaque, never derived from user data — and
services propagate an inbound ID rather than generating a fresh one
mid-chain, so one ID follows a request across every service.
"""

import functools
import inspect
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar

from asgi_correlation_id import correlation_id

from .errors import MessageAttributeLimitError
from .tracing import (
    attach_context,
    context_from_carrier,
    current_context,
    detach_context,
    drop_or_raise,
    inject_trace_context,
)

if TYPE_CHECKING:
    from opentelemetry.context import Context

__all__ = [
    "bind_correlation_id",
    "bound_correlation_id",
    "ensure_correlation_id",
    "outbound_headers",
    "sqs_message_attributes",
    "extract_correlation_id",
    "extract_trace_context",
    "bind_from_sqs_message",
    "bound_message_context",
    "with_correlation",
]

DEFAULT_HEADER = "X-Request-ID"

# The trace context bind_from_sqs_message last attached, paired with the
# context that was current before it.
_sqs_attached: "ContextVar[tuple[Context, Context] | None]" = ContextVar(
    "auditry_sqs_attached", default=None
)
SQS_ATTRIBUTE_NAME = "correlation_id"

# W3C Trace Context fields, used verbatim as header and message-attribute
# names (the same names OTel's botocore/boto3sqs instrumentations use).
TRACE_CONTEXT_FIELDS = ("traceparent", "tracestate")

# The SQS per-message attribute quota.
SQS_MAX_MESSAGE_ATTRIBUTES = 10

# The header name from ObservabilityConfig.correlation_id_header, seeded by
# create_middleware(), so outbound calls use the same name the service
# accepts inbound. DEFAULT_HEADER is the fallback for processes without
# middleware (workers, scripts).
_config_header: str | None = None

F = TypeVar("F", bound=Callable[..., Any])


def _new_id() -> str:
    # Same shape as the ID the ASGI middleware generates (asgi-correlation-id
    # uses uuid4().hex), so IDs look alike whichever side minted them.
    return uuid.uuid4().hex


def _set_correlation_header(name: str | None) -> None:
    """Seed the outbound header name from ObservabilityConfig (called by
    create_middleware). ``None`` clears it (tests)."""
    global _config_header
    _config_header = name


def correlation_header_name() -> str:
    """The header name carrying the correlation ID — the configured
    ``correlation_id_header`` when middleware exists, else ``X-Request-ID``."""
    return _config_header or DEFAULT_HEADER


def bind_correlation_id(value: str | None = None) -> str:
    """
    Bind a correlation ID to the current execution context — and leave it
    bound.

    For a true edge: a script, a one-shot process, a startup hook. Pass the
    inbound ID when one exists (propagate, never regenerate); omit it only
    where nothing upstream had one, and a random UUID4 is generated.

    The binding is sticky: it lasts for the rest of the context's life. In a
    long-lived worker that is the wrong tool — the last job's ID would be
    logged against everything that runs next. Use ``with_correlation`` or
    ``bound_correlation_id()`` there; both restore the previous context when
    the unit of work ends.

    Returns the bound ID.
    """
    cid = value or _new_id()
    correlation_id.set(cid)
    return cid


@contextmanager
def bound_correlation_id(value: str | None = None) -> Iterator[str]:
    """
    Bind a correlation ID for the duration of a block, then restore whatever
    was bound before.

    ```python
    for message in receive():
        with bound_correlation_id(extract_correlation_id(message)):
            logger.info("processing")   # carries this message's ID
    # ... and nothing after the block does.
    ```

    Pass the inbound ID when one exists; omit it to generate a random one.
    Yields the bound ID. This is what ``with_correlation`` does for a task
    function; use this form when the unit of work is not a function.
    """
    cid = value or _new_id()
    token = correlation_id.set(cid)
    try:
        yield cid
    finally:
        correlation_id.reset(token)


def ensure_correlation_id() -> str:
    """Return the current correlation ID, binding a fresh random one if unset."""
    return correlation_id.get() or bind_correlation_id()


def outbound_headers(
    header_name: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Headers for an outbound HTTP call, carrying the correlation ID.

    ```python
    resp = await client.post(url, json=payload, headers=outbound_headers())
    ```

    The header name defaults to the service's configured
    ``correlation_id_header`` (seeded by ``create_middleware``), so a service
    that customizes the inbound header sends the same name outbound. Pass
    ``header_name=`` to override per call.

    Also attach these to third-party API calls where the SDK allows custom
    headers/metadata, so vendor-side audit trails line up with your logs.

    Inside a valid span (``otel`` extra installed), also carries
    ``traceparent`` and, when non-empty, ``tracestate``, so the downstream
    service continues the trace. ``baggage`` is never sent. A trace header
    already in ``extra`` (any letter case) is kept as given.
    """
    headers = dict(extra) if extra else {}
    headers[header_name or correlation_header_name()] = ensure_correlation_id()
    present = {name.lower() for name in headers}
    for name, value in _trace_carrier().items():
        if name not in present:
            headers[name] = value
    return headers


def sqs_message_attributes(
    existing: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """
    SQS/SNS MessageAttributes carrying the correlation ID.

    ```python
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(job),
        MessageAttributes=sqs_message_attributes(),
    )
    ```

    Inside a valid span (``otel`` extra installed), also carries the
    ``traceparent`` and ``tracestate`` attributes, unless that would exceed
    the SQS limit of 10 attributes per message: then the trace attributes
    are skipped with one warning per process (``MessageAttributeLimitError``
    in strict mode). Attributes already in ``existing`` are never overwritten
    by trace context.
    """
    attrs = dict(existing) if existing else {}
    attrs[SQS_ATTRIBUTE_NAME] = _string_attribute(ensure_correlation_id())
    trace_fields = {name: value for name, value in _trace_carrier().items() if name not in attrs}
    if trace_fields and len(attrs) + len(trace_fields) > SQS_MAX_MESSAGE_ATTRIBUTES:
        drop_or_raise(
            "sqs_message_attributes",
            MessageAttributeLimitError(
                f"{len(attrs)} message attributes plus {len(trace_fields)} for trace "
                f"context exceeds the SQS limit of {SQS_MAX_MESSAGE_ATTRIBUTES}"
            ),
        )
        return attrs
    for name, value in trace_fields.items():
        attrs[name] = _string_attribute(value)
    return attrs


def _string_attribute(value: str) -> dict[str, str]:
    return {"DataType": "String", "StringValue": value}


def _trace_carrier() -> dict[str, str]:
    """The current span's W3C trace context; empty outside a valid span."""
    carrier: dict[str, str] = {}
    inject_trace_context(carrier)
    return carrier


def _message_attribute(message: Mapping[str, Any], name: str) -> str | None:
    """A string attribute of a received message: ``MessageAttributes`` /
    ``StringValue`` from a boto3 receive, or ``messageAttributes`` /
    ``stringValue`` from a Lambda event record."""
    attrs = message.get("MessageAttributes") or message.get("messageAttributes") or {}
    attr = attrs.get(name) or {}
    return attr.get("StringValue") or attr.get("stringValue")


def extract_correlation_id(message: dict[str, Any]) -> str | None:
    """The correlation ID carried by a received SQS/SNS message, or ``None``.
    Binds nothing — pair it with ``bound_correlation_id()`` in a consumer loop.
    """
    return _message_attribute(message, SQS_ATTRIBUTE_NAME)


def extract_trace_context(message: dict[str, Any]) -> "Context | None":
    """The trace context carried by a received SQS/SNS message's
    ``traceparent`` / ``tracestate`` attributes, or ``None`` when it carries no
    valid one (or the ``otel`` extra is not installed). Attaches nothing; see
    ``bound_message_context()``."""
    carrier = {}
    for name in TRACE_CONTEXT_FIELDS:
        value = _message_attribute(message, name)
        if value:
            carrier[name] = value
    return context_from_carrier(carrier) if carrier else None


@contextmanager
def bound_message_context(message: dict[str, Any]) -> Iterator[str]:
    """
    Bind a received SQS/SNS message's correlation ID and attach its trace
    context for the duration of a block, then restore both.

    ```python
    for message in response.get("Messages", []):
        with bound_message_context(message):
            logger.info("processing")   # this message's correlation_id and trace_id
    ```

    A message without a correlation ID gets a fresh random one. A message
    without a valid trace context leaves the current one in place. Yields the
    bound correlation ID.
    """
    with bound_correlation_id(extract_correlation_id(message)) as cid:
        token = attach_context(extract_trace_context(message))
        try:
            yield cid
        finally:
            detach_context(token)


def bind_from_sqs_message(message: dict[str, Any]) -> str:
    """
    Extract the correlation ID from a received SQS message and bind it —
    call this BEFORE the consumer's first log line.

    Falls back to a fresh random ID if the attribute is absent, so a log
    line is never emitted without one. Also attaches the message's trace
    context, when it carries a valid one. Both bindings are sticky (see
    ``bind_correlation_id``) until the next call, which replaces them: a
    message without trace context does not inherit the previous message's.
    In a long-lived consumer, wrap each message in
    ``bound_message_context(message)`` instead.
    """
    cid = bind_correlation_id(extract_correlation_id(message))
    # Undo the previous attach only while it is still current. A context the
    # consumer entered since (a span per poll batch) is left in place. Setting
    # the earlier context, rather than detaching a token, also works in a task
    # whose context was copied from the one that attached.
    attached = _sqs_attached.get()
    if attached is not None and current_context() is attached[1]:
        attach_context(attached[0])
    before = current_context()
    ctx = extract_trace_context(message)
    attach_context(ctx)
    _sqs_attached.set(None if before is None or ctx is None else (before, ctx))
    return cid


def with_correlation(func: F) -> F:
    """
    Decorator for worker task functions (sync or async): binds the correlation
    ID before the task body runs, so every log line inside carries it.

    The ID is taken, in order, from a ``correlation_id`` keyword argument
    when the caller passes one (the producer got it from
    ``ensure_correlation_id()``), else from the ID already bound in the
    current context (e.g. by ``bound_message_context`` in a consumer loop),
    else a fresh random ID. An ID that is already in flight is propagated,
    never replaced. The kwarg is left in place if the wrapped function
    accepts it, and stripped otherwise.

    The binding is scoped to the task: the previous correlation context is
    restored when the task returns or raises, so a long-lived worker never
    logs a completed task's ID against later, unrelated work.

    ```python
    @with_correlation
    async def process_job(ctx, job_spec, correlation_id=None): ...
    ```
    """
    params = inspect.signature(func).parameters
    accepts_kwarg = "correlation_id" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )

    def _bind(kwargs: dict[str, Any]) -> Any:
        cid = kwargs.get("correlation_id") or correlation_id.get() or _new_id()
        token = correlation_id.set(cid)
        if not accepts_kwarg:
            kwargs.pop("correlation_id", None)
        return token

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _bind(kwargs)
            try:
                return await func(*args, **kwargs)
            finally:
                correlation_id.reset(token)

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _bind(kwargs)
        try:
            return func(*args, **kwargs)
        finally:
            correlation_id.reset(token)

    return sync_wrapper  # type: ignore[return-value]
