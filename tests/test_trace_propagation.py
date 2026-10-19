"""W3C trace context through auditry's outbound HTTP and SQS/SNS helpers."""

import contextvars

import pytest
from asgi_correlation_id import correlation_id

from auditry import (
    bind_from_sqs_message,
    bound_message_context,
    configure_logging,
    extract_trace_context,
    get_logger,
    outbound_headers,
    sqs_message_attributes,
)
from auditry.errors import MessageAttributeLimitError

pytest.importorskip("opentelemetry.sdk")
trace = pytest.importorskip("opentelemetry.trace")
baggage = pytest.importorskip("opentelemetry.baggage")
otel_context = pytest.importorskip("opentelemetry.context")

pytestmark = pytest.mark.usefixtures("clean_logging")


def trace_id_of(span):
    return trace.format_trace_id(span.get_span_context().trace_id)


def traceparent_of(span):
    context = span.get_span_context()
    trace_id = trace.format_trace_id(context.trace_id)
    span_id = trace.format_span_id(context.span_id)
    return f"00-{trace_id}-{span_id}-{context.trace_flags:02x}"


def received(attributes):
    """A boto3 receive_message record carrying ``attributes``."""
    return {"MessageId": "m-1", "Body": "{}", "MessageAttributes": attributes}


class TestOutboundHeaders:
    def test_inside_a_span_carries_traceparent(self, otel):
        with otel.tracer.start_as_current_span("call") as span:
            headers = outbound_headers()
        assert headers["traceparent"] == traceparent_of(span)
        assert "X-Request-ID" in headers

    def test_outside_a_span_carries_no_trace_headers(self):
        headers = outbound_headers()
        assert set(headers) == {"X-Request-ID"}

    @pytest.mark.parametrize("name", ["traceparent", "Traceparent", "TRACEPARENT"])
    def test_caller_supplied_traceparent_wins(self, otel, name):
        mine = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
        with otel.tracer.start_as_current_span("call"):
            headers = outbound_headers(extra={name: mine})
        assert [key for key in headers if key.lower() == "traceparent"] == [name]
        assert headers[name] == mine

    def test_baggage_is_never_sent(self, otel):
        token = otel_context.attach(baggage.set_baggage("user", "jane@example.com"))
        try:
            with otel.tracer.start_as_current_span("call"):
                headers = outbound_headers()
        finally:
            otel_context.detach(token)
        assert "baggage" not in {key.lower() for key in headers}
        assert "traceparent" in headers


class TestSqsRoundTrip:
    def test_consumer_logs_the_producers_trace_id(self, otel, log_lines):
        configure_logging(service="svc")
        with otel.tracer.start_as_current_span("producer") as producer:
            attributes = sqs_message_attributes()
        assert attributes["traceparent"] == {
            "DataType": "String",
            "StringValue": traceparent_of(producer),
        }

        # The consumer is another process: nothing bound.
        correlation_id.set(None)
        with bound_message_context(received(attributes)) as cid:
            get_logger("consumer").info("processing")
        assert cid == attributes["correlation_id"]["StringValue"]
        assert not trace.get_current_span().get_span_context().is_valid
        assert correlation_id.get() is None

        (line,) = log_lines()
        assert line["trace_id"] == trace_id_of(producer)
        assert line["correlation_id"] == cid

    def test_previous_context_is_restored_after_the_block(self, otel):
        with otel.tracer.start_as_current_span("producer"):
            attributes = sqs_message_attributes()
        correlation_id.set("outer")
        with otel.tracer.start_as_current_span("consumer-loop") as outer:
            with bound_message_context(received(attributes)):
                assert trace.get_current_span() is not outer
            assert trace.get_current_span() is outer
        assert correlation_id.get() == "outer"

    def test_restored_when_the_block_raises(self, otel):
        with otel.tracer.start_as_current_span("producer"):
            attributes = sqs_message_attributes()
        correlation_id.set(None)
        with pytest.raises(RuntimeError):
            with bound_message_context(received(attributes)):
                raise RuntimeError("boom")
        assert not trace.get_current_span().get_span_context().is_valid
        assert correlation_id.get() is None

    def test_message_without_trace_context_keeps_the_current_one(self, otel):
        with otel.tracer.start_as_current_span("consumer-loop") as outer:
            with bound_message_context(received({})) as cid:
                assert trace.get_current_span() is outer
                assert correlation_id.get() == cid

    def test_lambda_event_casing(self, otel):
        with otel.tracer.start_as_current_span("producer") as producer:
            value = traceparent_of(producer)
        record = {"messageAttributes": {"traceparent": {"stringValue": value}}}
        ctx = extract_trace_context(record)
        assert ctx is not None
        assert trace.get_current_span(ctx).get_span_context().trace_id == (
            producer.get_span_context().trace_id
        )

    def test_invalid_traceparent_extracts_nothing(self):
        record = received({"traceparent": {"DataType": "String", "StringValue": "nope"}})
        assert extract_trace_context(record) is None

    def test_bind_from_sqs_message_attaches_sticky(self, otel):
        with otel.tracer.start_as_current_span("producer") as producer:
            attributes = sqs_message_attributes()

        def consume():
            bind_from_sqs_message(received(attributes))
            return trace.get_current_span().get_span_context().trace_id

        # A copied context keeps the sticky binding out of later tests.
        assert contextvars.copy_context().run(consume) == producer.get_span_context().trace_id


class TestSqsAttributeLimit:
    @staticmethod
    def nine_attributes():
        return {f"attr{i}": {"DataType": "String", "StringValue": "v"} for i in range(9)}

    def test_trace_attributes_skipped_with_one_warning(self, otel, log_lines):
        configure_logging(service="svc", environment="prod")
        with otel.tracer.start_as_current_span("producer"):
            first = sqs_message_attributes(self.nine_attributes())
            second = sqs_message_attributes(self.nine_attributes())
        for attributes in (first, second):
            assert "correlation_id" in attributes
            assert "traceparent" not in attributes
            assert len(attributes) == 10
        warnings = [line for line in log_lines() if line.get("operation")]
        assert [(w["operation"], w["error_type"]) for w in warnings] == [
            ("sqs_message_attributes", "MessageAttributeLimitError")
        ]

    def test_strict_mode_raises(self, otel):
        configure_logging(service="svc", strict=True)
        with otel.tracer.start_as_current_span("producer"):
            with pytest.raises(MessageAttributeLimitError):
                sqs_message_attributes(self.nine_attributes())

    def test_limit_applies_only_when_trace_attributes_are_added(self, log_lines):
        """Outside a span there is no trace context, so ten existing
        attributes plus correlation_id never trip the trace-context check."""
        configure_logging(service="svc", strict=True)
        existing = {f"attr{i}": {"DataType": "String", "StringValue": "v"} for i in range(10)}
        attributes = sqs_message_attributes(existing)
        assert set(attributes) == {*existing, "correlation_id"}
        assert not [line for line in log_lines() if line.get("operation")]

    def test_eight_existing_attributes_fit(self, otel):
        existing = {f"attr{i}": {"DataType": "String", "StringValue": "v"} for i in range(8)}
        with otel.tracer.start_as_current_span("producer"):
            attributes = sqs_message_attributes(existing)
        assert len(attributes) == 10
        assert "traceparent" in attributes

    def test_existing_trace_attribute_is_not_overwritten(self, otel):
        mine = {"DataType": "String", "StringValue": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}
        with otel.tracer.start_as_current_span("producer"):
            attributes = sqs_message_attributes({"traceparent": mine})
        assert attributes["traceparent"] == mine
