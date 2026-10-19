"""Tests for the pure span scrubbing rules. Nothing here imports OpenTelemetry."""

import pytest

from auditry import ObservabilityConfig
from auditry.span_scrubbing import (
    SpanScrubPolicy,
    get_span_scrub_policy,
    is_dropped_event,
    redact_emails,
    sanitize_sql,
    scrub_attributes,
    scrub_exception_event,
    scrub_query,
    scrub_span_name,
    scrub_target,
    scrub_url,
    set_span_scrub_policy,
)

R = "[REDACTED]"
DEFAULT = SpanScrubPolicy()
PAGE_ALLOWED = SpanScrubPolicy(query_param_allowlist=frozenset({"page"}))


def server(attributes, policy=DEFAULT):
    return scrub_attributes(attributes, is_server=True, policy=policy)


def client(attributes, policy=DEFAULT):
    return scrub_attributes(attributes, is_server=False, policy=policy)


class TestPolicy:
    def test_from_config(self):
        config = ObservabilityConfig(
            service_name="svc",
            log_request_body=False,
            log_response_body=False,
            additional_redaction_patterns=["tenant_secret"],
            tracing={
                "query_param_allowlist": ["page"],
                "db_statement": "drop",
                "keep_client_address": True,
            },
        )
        assert SpanScrubPolicy.from_config(config) == SpanScrubPolicy(
            query_param_allowlist=frozenset({"page"}),
            redaction_patterns=("tenant_secret",),
            db_statement="drop",
            keep_client_address=True,
        )

    def test_unknown_db_statement_mode_is_rejected(self):
        with pytest.raises(ValueError, match="db_statement"):
            SpanScrubPolicy(db_statement="redact")

    def test_registry_defaults_and_resets(self):
        custom = SpanScrubPolicy(keep_client_address=True)
        set_span_scrub_policy(custom)
        try:
            assert get_span_scrub_policy() is custom
        finally:
            set_span_scrub_policy(None)
        assert get_span_scrub_policy() == SpanScrubPolicy()


class TestExceptionEvents:
    def test_message_and_stacktrace_removed(self):
        event = {
            "exception.type": "ValueError",
            "exception.message": "cannot open q3.pdf for jane@example.com",
            "exception.stacktrace": "Traceback ...",
            "exception.escaped": "False",
        }
        assert scrub_exception_event(event) == {
            "exception.type": "ValueError",
            "exception.escaped": "False",
        }

    def test_other_keys_kept(self):
        assert scrub_exception_event({"exception.type": "KeyError"}) == {
            "exception.type": "KeyError"
        }


class TestQueryStrings:
    QUERY = "email=jane@example.com&token=abc&page=2"

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            (PAGE_ALLOWED, f"email={R}&token={R}&page=2"),
            (DEFAULT, f"email={R}&token={R}&page={R}"),
        ],
    )
    def test_only_allowlisted_values_survive(self, policy, expected):
        assert scrub_query(self.QUERY, policy) == expected

    def test_allowlisted_key_matching_a_redaction_pattern_is_redacted(self):
        policy = SpanScrubPolicy(query_param_allowlist=frozenset({"token", "page"}))
        assert scrub_query("token=abc&page=2", policy) == f"token={R}&page=2"

    def test_additional_redaction_patterns_apply(self):
        policy = SpanScrubPolicy(
            query_param_allowlist=frozenset({"tenant_ref"}), redaction_patterns=("tenant",)
        )
        assert scrub_query("tenant_ref=t-1", policy) == f"tenant_ref={R}"

    def test_blank_values_and_keys_are_kept(self):
        assert scrub_query("page=&q=x", PAGE_ALLOWED) == f"page=&q={R}"

    @pytest.mark.parametrize("query", ["q3-board-deck.pdf", "a=1&b", "a=1&&b=2"])
    def test_unparseable_query_is_redacted_whole(self, query):
        assert scrub_query(query, PAGE_ALLOWED) == R

    def test_empty_query(self):
        assert scrub_query("", DEFAULT) == ""

    def test_full_url_strips_userinfo(self):
        url = "https://user:pass@api.example.com:8443/v1/files?token=abc&page=2"
        assert scrub_url(url, PAGE_ALLOWED) == (
            f"https://api.example.com:8443/v1/files?token={R}&page=2"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://app.example.com/cb#access_token=abc&state=jane@example.com",
            "https://app.example.com/cb?#access_token=abc",
        ],
    )
    def test_full_url_loses_its_fragment(self, url):
        assert "#" not in scrub_url(url, DEFAULT)
        assert "access_token" not in scrub_url(url, DEFAULT)

    def test_fragment_attribute_is_removed(self):
        assert client({"url.fragment": "access_token=abc", "url.scheme": "https"}) == {
            "url.scheme": "https"
        }

    def test_target_loses_its_fragment(self):
        assert scrub_target("/cb?page=2#token=abc", PAGE_ALLOWED) == "/cb?page=2"
        assert scrub_target("/cb#token=abc", PAGE_ALLOWED) == "/cb"

    def test_full_url_without_query(self):
        assert scrub_url("http://host/files/a.pdf", DEFAULT) == "http://host/files/a.pdf"

    def test_unparseable_url_is_redacted_whole(self):
        assert scrub_url("http://[::1/x?token=abc", DEFAULT) == R

    def test_target(self):
        assert scrub_target("/files/a.pdf?token=abc&page=2", PAGE_ALLOWED) == (
            f"/files/a.pdf?token={R}&page=2"
        )

    @pytest.mark.parametrize("key", ["url.full", "http.url", "url.query", "http.target"])
    def test_every_url_key_on_any_span(self, key):
        value = {
            "url.full": "http://h/p?token=abc",
            "http.url": "http://h/p?token=abc",
            "url.query": "token=abc",
            "http.target": "/p?token=abc",
        }[key]
        assert "abc" not in client({key: value})[key]
        assert f"token={R}" in client({key: value})[key]

    def test_non_string_url_value_is_removed(self):
        assert client({"url.query": ("token=abc",)}) == {}


class TestServerPaths:
    ATTRIBUTES = {
        "http.route": "/files/{name}",
        "url.path": "/files/q3-board-deck.pdf",
        "http.target": "/files/q3-board-deck.pdf?page=2",
        "url.full": "http://host/files/q3-board-deck.pdf?page=2",
        "http.url": "http://host/files/q3-board-deck.pdf?page=2",
    }

    def test_paths_become_the_route(self):
        assert server(self.ATTRIBUTES, PAGE_ALLOWED) == {
            "http.route": "/files/{name}",
            "url.path": "/files/{name}",
            "http.target": "/files/{name}?page=2",
            "url.full": "http://host/files/{name}?page=2",
            "http.url": "http://host/files/{name}?page=2",
        }

    def test_client_spans_keep_their_path(self):
        scrubbed = client(self.ATTRIBUTES, PAGE_ALLOWED)
        assert scrubbed["url.path"] == "/files/q3-board-deck.pdf"
        assert scrubbed["http.target"] == "/files/q3-board-deck.pdf?page=2"

    def test_server_span_without_a_route_keeps_the_raw_path(self):
        scrubbed = server({"url.path": "/nope", "http.target": "/nope?x=1"})
        assert scrubbed == {"url.path": "/nope", "http.target": f"/nope?x={R}"}


class TestSpanNames:
    @pytest.mark.parametrize(
        ("name", "attributes", "expected"),
        [
            (
                "GET /files/q3.pdf",
                {"http.method": "GET", "http.route": "/files/{n}"},
                "GET /files/{n}",
            ),
            ("GET /files/q3.pdf", {"http.request.method": "GET"}, "GET"),
            ("x", {"http.request.method": "_OTHER", "http.route": "/a"}, "HTTP /a"),
            ("x", {"http.request.method": "_OTHER"}, "HTTP"),
            ("custom", {}, "custom"),
            ("GET /a?token=abc", {}, "GET /a"),
        ],
    )
    def test_server_names(self, name, attributes, expected):
        assert scrub_span_name(name, attributes, is_server=True) == expected

    def test_request_method_wins_over_legacy_method(self):
        attributes = {"http.request.method": "POST", "http.method": "GET"}
        assert scrub_span_name("x", attributes, is_server=True) == "POST"

    def test_client_names_are_only_cut_at_the_query(self):
        attributes = {"http.method": "GET", "http.route": "/r"}
        assert scrub_span_name("GET /a?b=c", attributes, is_server=False) == "GET /a"
        assert scrub_span_name("GET", attributes, is_server=False) == "GET"

    def test_emails_in_names_are_redacted(self):
        assert scrub_span_name("lookup jane@example.com", {}, is_server=False) == f"lookup {R}"


class TestClientAddress:
    ADDRESS = {
        "client.address": "203.0.113.7",
        "client.port": 51234,
        "net.peer.ip": "203.0.113.7",
        "net.peer.port": 51234,
        "network.peer.address": "203.0.113.7",
        "network.peer.port": 51234,
        "net.sock.peer.addr": "203.0.113.7",
        "net.sock.peer.port": 51234,
        "http.client_ip": "203.0.113.7",
    }

    def test_removed_on_server_spans(self):
        assert server({**self.ADDRESS, "server.address": "api"}) == {"server.address": "api"}

    def test_kept_when_the_policy_says_so(self):
        assert server(self.ADDRESS, SpanScrubPolicy(keep_client_address=True)) == self.ADDRESS

    def test_kept_on_client_spans(self):
        assert client(self.ADDRESS) == self.ADDRESS


class TestCapturedHeaders:
    @pytest.mark.parametrize(
        "key",
        [
            "http.request.header.authorization",
            "http.request.header.x-api-key",
            "http.request.header.x_api_key",
            "http.request.header.cookie",
            "http.response.header.set_cookie",
            "http.request.header.x_auth_user",
        ],
    )
    def test_sensitive_headers_redacted(self, key):
        assert client({key: ("secret",)}) == {key: (R,)}

    def test_other_headers_kept(self):
        attributes = {"http.request.header.accept": ("application/json",)}
        assert client(attributes) == attributes

    def test_additional_patterns_apply_to_headers(self):
        policy = SpanScrubPolicy(redaction_patterns=("x-tenant",))
        assert client({"http.request.header.x_tenant": ("t-1",)}, policy) == {
            "http.request.header.x_tenant": (R,)
        }


class TestUserIdentity:
    def test_identity_removed_user_agent_kept(self):
        scrubbed = client(
            {
                "enduser.id": "u-1",
                "user.email": "jane@example.com",
                "user.id": "u-1",
                "user_agent.original": "curl/8",
                "http.user_agent": "curl/8",
            }
        )
        assert scrubbed == {"user_agent.original": "curl/8", "http.user_agent": "curl/8"}


class TestEmails:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("jane@example.com", R),
            ("to jane.doe+x@mail.example.co.uk now", f"to {R} now"),
            ("q=jane%40example.com", f"q={R}"),
            ("no address here", "no address here"),
            ("user@localhost", "user@localhost"),
            (("a@b.io", "plain"), (R, "plain")),
            (b"jane@example.com", R.encode()),
            ({"nested": ["jane@example.com"]}, {"nested": (R,)}),
            (42, 42),
        ],
    )
    def test_values(self, value, expected):
        assert redact_emails(value) == expected

    def test_runs_on_every_attribute(self):
        assert client({"messaging.destination.name": "jane@example.com"}) == {
            "messaging.destination.name": R
        }

    def test_long_value_without_an_address_is_fast(self):
        value = "a" * 200_000 + "@"
        assert redact_emails(value) == value


class TestDatabaseStatements:
    STATEMENT = (
        "SELECT * FROM t WHERE email = 'jane@x.com' AND age > 30 AND id = $1 "
        "AND col1 = 2 AND note = 'it''s'"
    )
    SANITIZED = "SELECT * FROM t WHERE email = ? AND age > ? AND id = $1 AND col1 = ? AND note = ?"

    def test_sanitize(self):
        assert sanitize_sql(self.STATEMENT) == self.SANITIZED

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("SELECT a FROM table2 WHERE x = %s AND y = :name AND z = ?", None),
            ("UPDATE t SET price = 1.5 WHERE id = 7", "UPDATE t SET price = ? WHERE id = ?"),
            ("SELECT s.col FROM schema1.t s", None),
        ],
    )
    def test_placeholders_and_identifiers_kept(self, statement, expected):
        assert sanitize_sql(statement) == (expected or statement)

    @pytest.mark.parametrize(
        "statement",
        [
            r"SELECT 1 FROM t WHERE note = 'it\'s' AND ssn = 'secret'",
            r"SELECT 'C:\' FROM t WHERE ssn = 'secret'",
            "SELECT 1 FROM t WHERE ssn = 'unterminated",
        ],
    )
    def test_ambiguous_literals_drop_the_statement(self, statement):
        assert sanitize_sql(statement) is None
        assert client({"db.statement": statement, "db.system": "mysql"}) == {"db.system": "mysql"}
        keep = SpanScrubPolicy(db_statement="keep")
        assert client({"db.statement": statement}, keep) == {"db.statement": statement}

    @pytest.mark.parametrize("key", ["db.statement", "db.query.text"])
    def test_modes(self, key):
        attributes = {key: self.STATEMENT, "db.query.parameter.0": "jane", "db.system": "pg"}
        assert client(attributes) == {key: self.SANITIZED, "db.system": "pg"}
        drop = SpanScrubPolicy(db_statement="drop")
        assert client(attributes, drop) == {"db.system": "pg"}
        keep = SpanScrubPolicy(db_statement="keep")
        # keep exports the statement as is, apart from email redaction.
        assert client(attributes, keep) == {
            key: self.STATEMENT.replace("jane@x.com", R),
            "db.system": "pg",
        }


class TestGenAI:
    def test_content_removed_metadata_kept(self):
        scrubbed = client(
            {
                "gen_ai.input.messages": '[{"role":"user","content":"hi"}]',
                "gen_ai.output.messages": "[]",
                "gen_ai.system_instructions": "be nice",
                "gen_ai.prompt": "hi",
                "gen_ai.prompt.0.content": "hi",
                "gen_ai.completion.0.content": "hello",
                "gen_ai.tool.call.arguments": "{}",
                "gen_ai.tool.call.result": "{}",
                "gen_ai.retrieval.query.text": "q3 board deck",
                "gen_ai.request.model": "claude-opus-5-5",
                "gen_ai.usage.input_tokens": 12,
                "gen_ai.response.finish_reasons": ("stop",),
                "gen_ai.operation.name": "chat",
            }
        )
        assert scrubbed == {
            "gen_ai.request.model": "claude-opus-5-5",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.response.finish_reasons": ("stop",),
            "gen_ai.operation.name": "chat",
        }

    @pytest.mark.parametrize(
        ("name", "dropped"),
        [("gen_ai.choice", True), ("gen_ai.user.message", True), ("exception", False)],
    )
    def test_events(self, name, dropped):
        assert is_dropped_event(name) is dropped


class TestKeepList:
    KEEP = {
        "http.route": "/files/{name}",
        "http.request.method": "GET",
        "http.method": "GET",
        "http.response.status_code": 500,
        "http.status_code": 500,
        "error.type": "ValueError",
        "exception.type": "ValueError",
        "correlation_id": "0f0e0d0c0b0a09080706050403020100",
        "server.address": "api.example.com",
        "user_agent.original": "curl/8",
        "http.user_agent": "curl/8",
        "db.system": "postgresql",
        "db.operation": "SELECT",
        "db.operation.name": "SELECT",
        "messaging.system": "aws_sqs",
        "messaging.operation": "publish",
        "gen_ai.request.model": "claude-opus-5-5",
        "gen_ai.usage.input_tokens": 12,
        "gen_ai.usage.output_tokens": 34,
    }

    @pytest.mark.parametrize("is_server", [True, False])
    def test_operational_values_survive(self, is_server):
        policy = SpanScrubPolicy()
        assert scrub_attributes(self.KEEP, is_server=is_server, policy=policy) == self.KEEP
