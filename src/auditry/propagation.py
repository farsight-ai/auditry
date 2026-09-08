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
- SQS/SNS: ``sqs_message_attributes()`` on send, ``bind_from_sqs_message()``
  on receive.

IDs are always random UUID4s — opaque, never derived from user data — and
services propagate an inbound ID rather than generating a fresh one
mid-chain, so one ID follows a request across every service.
"""

import functools
import inspect
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar

from asgi_correlation_id import correlation_id

__all__ = [
    "bind_correlation_id",
    "bound_correlation_id",
    "ensure_correlation_id",
    "outbound_headers",
    "sqs_message_attributes",
    "bind_from_sqs_message",
    "with_correlation",
]

DEFAULT_HEADER = "X-Request-ID"
SQS_ATTRIBUTE_NAME = "correlation_id"

# The header name from ObservabilityConfig.correlation_id_header, seeded by
# create_middleware(), so outbound calls use the same name the service
# accepts inbound. DEFAULT_HEADER is the fallback for processes without
# middleware (workers, scripts).
_config_header: Optional[str] = None

F = TypeVar("F", bound=Callable[..., Any])


def _set_correlation_header(name: Optional[str]) -> None:
    """Seed the outbound header name from ObservabilityConfig (called by
    create_middleware). ``None`` clears it (tests)."""
    global _config_header
    _config_header = name


def correlation_header_name() -> str:
    """The header name carrying the correlation ID — the configured
    ``correlation_id_header`` when middleware exists, else ``X-Request-ID``."""
    return _config_header or DEFAULT_HEADER


def bind_correlation_id(value: Optional[str] = None) -> str:
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
    cid = value or str(uuid.uuid4())
    correlation_id.set(cid)
    return cid


@contextmanager
def bound_correlation_id(value: Optional[str] = None) -> Iterator[str]:
    """
    Bind a correlation ID for the duration of a block, then restore whatever
    was bound before.

    ```python
    for message in receive():
        with bound_correlation_id(extract_id(message)):
            logger.info("processing")   # carries this message's ID
    # ... and nothing after the block does.
    ```

    Pass the inbound ID when one exists; omit it to generate a random one.
    Yields the bound ID. This is what ``with_correlation`` does for a task
    function; use this form when the unit of work is not a function.
    """
    cid = value or str(uuid.uuid4())
    token = correlation_id.set(cid)
    try:
        yield cid
    finally:
        correlation_id.reset(token)


def ensure_correlation_id() -> str:
    """Return the current correlation ID, binding a fresh random one if unset."""
    return correlation_id.get() or bind_correlation_id()


def outbound_headers(
    header_name: Optional[str] = None,
    extra: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
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
    """
    headers = dict(extra) if extra else {}
    headers[header_name or correlation_header_name()] = ensure_correlation_id()
    return headers


def sqs_message_attributes(
    existing: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    """
    SQS/SNS MessageAttributes carrying the correlation ID.

    ```python
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(job),
        MessageAttributes=sqs_message_attributes(),
    )
    ```
    """
    attrs = dict(existing) if existing else {}
    attrs[SQS_ATTRIBUTE_NAME] = {
        "DataType": "String",
        "StringValue": ensure_correlation_id(),
    }
    return attrs


def bind_from_sqs_message(message: Dict[str, Any]) -> str:
    """
    Extract the correlation ID from a received SQS message and bind it —
    call this BEFORE the consumer's first log line.

    Falls back to a fresh random ID if the attribute is absent, so a log
    line is never emitted without one. The binding is sticky (see
    ``bind_correlation_id``); in a long-lived consumer, wrap each message in
    ``bound_correlation_id(...)`` with the extracted value instead.
    """
    attrs = message.get("MessageAttributes") or message.get("messageAttributes") or {}
    attr = attrs.get(SQS_ATTRIBUTE_NAME) or {}
    value = attr.get("StringValue") or attr.get("stringValue")
    return bind_correlation_id(value)


def with_correlation(func: F) -> F:
    """
    Decorator for worker task functions (sync or async): binds the correlation
    ID before the task body runs, so every log line inside carries it.

    The ID is taken, in order, from a ``correlation_id`` keyword argument
    when the caller passes one (the producer got it from
    ``ensure_correlation_id()``), else from the ID already bound in the
    current context (e.g. by ``bind_from_sqs_message`` in a consumer loop),
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

    def _bind(kwargs: Dict[str, Any]) -> Any:
        cid = kwargs.get("correlation_id") or correlation_id.get() or str(uuid.uuid4())
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
