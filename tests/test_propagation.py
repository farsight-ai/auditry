"""Tests for auditry.propagation: correlation IDs surviving worker, outbound
HTTP, and SQS/SNS boundaries."""

import asyncio
import uuid

from asgi_correlation_id import correlation_id

from auditry.propagation import (
    bind_correlation_id,
    bind_from_sqs_message,
    ensure_correlation_id,
    outbound_headers,
    sqs_message_attributes,
    with_correlation,
)


def setup_function(_):
    correlation_id.set(None)


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
