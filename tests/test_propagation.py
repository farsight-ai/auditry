"""Tests for auditry.propagation: correlation IDs surviving worker, outbound
HTTP, and SQS/SNS boundaries."""

import asyncio
import uuid

import pytest
from asgi_correlation_id import correlation_id

from auditry.propagation import (
    _set_correlation_header,
    bind_correlation_id,
    bind_from_sqs_message,
    bound_correlation_id,
    correlation_header_name,
    ensure_correlation_id,
    outbound_headers,
    sqs_message_attributes,
    with_correlation,
)


@pytest.fixture(autouse=True)
def _clean_correlation_context():
    """Isolate every test's correlation context — start unbound, and restore
    the ambient value afterward so nothing this module binds (e.g. "outer")
    leaks into later tests. An autouse fixture rather than setup_function,
    because setup_function never runs for the class-based tests below."""
    token = correlation_id.set(None)
    _set_correlation_header(None)
    yield
    correlation_id.reset(token)
    _set_correlation_header(None)


class TestScopedBinding:
    """bound_correlation_id() binds for a block and restores the previous
    context — the form a long-lived worker should use, so a finished unit
    of work never leaks its ID onto what runs next."""

    def test_restores_previous_context(self):
        bind_correlation_id("outer")
        with bound_correlation_id("inner") as cid:
            assert cid == "inner"
            assert correlation_id.get() == "inner"
        assert correlation_id.get() == "outer"

    def test_restores_unbound_context(self):
        with bound_correlation_id("only"):
            assert correlation_id.get() == "only"
        assert correlation_id.get() is None

    def test_restores_on_raise(self):
        bind_correlation_id("outer")
        with pytest.raises(RuntimeError):
            with bound_correlation_id("inner"):
                raise RuntimeError("boom")
        assert correlation_id.get() == "outer"

    def test_generates_when_absent(self):
        with bound_correlation_id() as cid:
            assert uuid.UUID(cid).version == 4
            assert correlation_id.get() == cid

    def test_sticky_bind_is_documented_as_sticky(self):
        # The plain helper stays sticky (its contract for edges); the scoped
        # form is the one that cleans up.
        bind_correlation_id("sticky")
        assert correlation_id.get() == "sticky"


class TestHeaderName:
    """outbound_headers() defaults to the configured correlation_id_header,
    seeded by create_middleware, so a service that customizes the inbound
    header sends the same name outbound."""

    def test_default_without_config(self):
        assert correlation_header_name() == "X-Request-ID"
        bind_correlation_id("abc")
        assert outbound_headers() == {"X-Request-ID": "abc"}

    def test_configured_header_is_used(self):
        _set_correlation_header("X-Trace-Id")
        bind_correlation_id("abc")
        assert correlation_header_name() == "X-Trace-Id"
        assert outbound_headers() == {"X-Trace-Id": "abc"}

    def test_explicit_header_name_still_wins(self):
        _set_correlation_header("X-Trace-Id")
        bind_correlation_id("abc")
        assert outbound_headers(header_name="X-Job-Id") == {"X-Job-Id": "abc"}


class TestBinding:
    def test_bind_explicit_value_propagates_not_regenerates(self):
        # An inbound ID is propagated, never replaced.
        assert bind_correlation_id("inbound-id") == "inbound-id"
        assert correlation_id.get() == "inbound-id"

    def test_bind_generates_random_uuid4(self):
        # Generated IDs are random and opaque.
        cid = bind_correlation_id()
        assert uuid.UUID(cid).version == 4

    def test_ensure_reuses_existing(self):
        bind_correlation_id("existing")
        assert ensure_correlation_id() == "existing"

    def test_ensure_binds_when_missing(self):
        cid = ensure_correlation_id()
        assert cid
        assert correlation_id.get() == cid


class TestOutbound:
    def test_outbound_headers_carry_id(self):
        bind_correlation_id("abc-123")
        headers = outbound_headers()
        assert headers["X-Request-ID"] == "abc-123"

    def test_outbound_headers_custom_name_and_extra(self):
        bind_correlation_id("abc-123")
        headers = outbound_headers(header_name="X-Job-Id", extra={"Content-Type": "application/json"})
        assert headers["X-Job-Id"] == "abc-123"
        assert headers["Content-Type"] == "application/json"


class TestSqs:
    def test_message_attributes_roundtrip(self):
        bind_correlation_id("queue-id-1")
        attrs = sqs_message_attributes()
        assert attrs["correlation_id"]["StringValue"] == "queue-id-1"

        correlation_id.set(None)
        message = {"MessageAttributes": attrs, "Body": "{}"}
        assert bind_from_sqs_message(message) == "queue-id-1"
        assert correlation_id.get() == "queue-id-1"

    def test_merges_existing_attributes(self):
        bind_correlation_id("queue-id-2")
        attrs = sqs_message_attributes({"other": {"DataType": "String", "StringValue": "x"}})
        assert "other" in attrs and "correlation_id" in attrs

    def test_missing_attribute_binds_fresh_id(self):
        # A consumer never logs without an ID.
        cid = bind_from_sqs_message({"Body": "{}"})
        assert uuid.UUID(cid).version == 4


class TestDecorator:
    def test_sync_function_binds_from_kwarg(self):
        @with_correlation
        def task(data, correlation_id=None):
            return correlation_id, globals_cid()

        def globals_cid():
            return correlation_id.get()

        passed, bound = task("x", correlation_id="job-77")
        assert passed == "job-77"
        assert bound == "job-77"

    def test_sync_function_strips_kwarg_when_not_accepted(self):
        @with_correlation
        def task(data):
            return correlation_id.get()

        assert task("x", correlation_id="job-88") == "job-88"

    def test_async_function_binds(self):
        @with_correlation
        async def task(data, correlation_id=None):
            # The parameter shadows the module-level ContextVar, so read the
            # bound value through a helper (as the sync case above does).
            return correlation_id, globals_cid()

        def globals_cid():
            return correlation_id.get()

        passed, bound = asyncio.run(task("x", correlation_id="job-99"))
        assert passed == "job-99"
        assert bound == "job-99"

    def test_generates_when_absent(self):
        @with_correlation
        def task(data):
            return correlation_id.get()

        assert uuid.UUID(task("x")).version == 4

    def test_propagates_an_id_already_bound_in_context(self):
        """The documented SQS pattern: bind_from_sqs_message() in the consumer
        loop, then a decorated handler. The handler must continue that
        trace, not start a new one."""
        bind_from_sqs_message({
            "MessageAttributes": {
                "correlation_id": {"DataType": "String", "StringValue": "from-queue"}
            }
        })

        @with_correlation
        def handler(data):
            return correlation_id.get()

        assert handler("x") == "from-queue"

    def test_explicit_kwarg_beats_bound_context(self):
        bind_correlation_id("ambient")

        @with_correlation
        def handler(data):
            return correlation_id.get()

        assert handler("x", correlation_id="explicit") == "explicit"
        assert correlation_id.get() == "ambient"  # and restored afterwards


class TestDecoratorScoping:
    """The binding is task-scoped: the surrounding context is restored when
    the task returns or raises, so a long-lived worker never logs a finished
    task's ID against later work."""

    def test_context_restored_after_return(self):
        bind_correlation_id("outer")

        @with_correlation
        def task(data, correlation_id=None):
            return correlation_id

        assert task("x", correlation_id="inner") == "inner"
        assert correlation_id.get() == "outer"

    def test_context_restored_after_raise(self):
        bind_correlation_id("outer")

        @with_correlation
        def task(data, correlation_id=None):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            task("x", correlation_id="inner")
        assert correlation_id.get() == "outer"

    def test_async_context_restored(self):
        bind_correlation_id("outer")

        @with_correlation
        async def task(data, correlation_id=None):
            return correlation_id.upper()

        assert asyncio.run(task("x", correlation_id="inner")) == "INNER"
        assert correlation_id.get() == "outer"

    def test_var_keyword_functions_keep_the_kwarg(self):
        """A function that only takes **kwargs still receives correlation_id."""
        seen = {}

        @with_correlation
        def task(data, **kwargs):
            seen.update(kwargs)
            return correlation_id.get()

        assert task("x", correlation_id="kw-1") == "kw-1"
        assert seen["correlation_id"] == "kw-1"
