"""Tests for the logging schema, error discipline, and config defaults."""

from auditry import ObservabilityConfig


class TestQueryParamRedaction:
    """A token in a query string must not bypass redaction."""

    def test_query_params_redacted(self):
        from auditry.core.logger import RequestResponseLogger

        cfg = ObservabilityConfig(
            service_name="svc", log_request_body=False, log_response_body=False
        )
        rrl = RequestResponseLogger(cfg)
        prepared = rrl.prepare_request_data(
            {
                "method": "GET",
                "path": "/x",
                "query_params": {"token": "sekrit", "page": "2"},
            },
            correlation_id="cid",
        )
        assert prepared["query_params"]["token"] == "[REDACTED]"
        assert prepared["query_params"]["page"] == "2"
