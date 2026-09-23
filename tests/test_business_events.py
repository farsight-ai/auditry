"""
Tests for business event matching and context extraction.

Business events are a documented feature (see the README) but nothing exercised
them: the middlewares reach them only through RequestResponseLogger, which is
the framework-agnostic seam these tests drive directly.
"""

import pytest

from auditry import ObservabilityConfig
from auditry.core.logger import RequestResponseLogger
from auditry.models import BusinessEventConfig


def make_logger(**business_events):
    """A logger configured with the given {pattern: BusinessEventConfig}."""
    return RequestResponseLogger(
        ObservabilityConfig(service_name="test-business-events", business_events=business_events)
    )


@pytest.fixture
def folder_logger():
    return make_logger(
        **{
            "POST /folders": BusinessEventConfig(
                event_type="folder.created",
                extract_from_request=["name", "parent_id"],
                extract_from_response=["id"],
            ),
            "DELETE /folders/{folder_id}": BusinessEventConfig(
                event_type="folder.deleted",
                extract_from_path=["folder_id"],
            ),
        }
    )


# ================= matching =================


def test_no_configured_events_matches_nothing():
    logger = make_logger()

    assert logger._extract_business_event({"method": "POST", "path": "/folders"}, {}) == (
        None,
        None,
    )


def test_unmatched_path_returns_no_event(folder_logger):
    assert folder_logger._extract_business_event({"method": "GET", "path": "/health"}, {}) == (
        None,
        None,
    )


def test_method_must_match_as_well_as_path(folder_logger):
    # Same path as the configured "POST /folders", different verb.
    assert folder_logger._extract_business_event({"method": "GET", "path": "/folders"}, {}) == (
        None,
        None,
    )


def test_path_parameter_pattern_matches_a_concrete_path(folder_logger):
    event_type, _ = folder_logger._extract_business_event(
        {"method": "DELETE", "path": "/folders/42", "path_params": {"folder_id": "42"}}, {}
    )

    assert event_type == "folder.deleted"


# ================= context extraction =================


def test_context_is_extracted_from_the_request_body(folder_logger):
    event_type, context = folder_logger._extract_business_event(
        {"method": "POST", "path": "/folders", "body": {"name": "reports", "parent_id": "7"}},
        {},
    )

    assert event_type == "folder.created"
    assert context == {"name": "reports", "parent_id": "7"}


def test_context_is_extracted_from_the_response_body(folder_logger):
    _, context = folder_logger._extract_business_event(
        {"method": "POST", "path": "/folders", "body": {"name": "reports"}},
        {"body": {"id": "folder-99", "created_at": "2026-01-01"}},
    )

    # Only the configured field is pulled across, not the whole body.
    assert context == {"name": "reports", "id": "folder-99"}


def test_context_is_extracted_from_path_params(folder_logger):
    _, context = folder_logger._extract_business_event(
        {"method": "DELETE", "path": "/folders/42", "path_params": {"folder_id": "42"}}, {}
    )

    assert context == {"folder_id": "42"}


def test_fields_missing_from_the_body_are_skipped(folder_logger):
    _, context = folder_logger._extract_business_event(
        {"method": "POST", "path": "/folders", "body": {"name": "reports"}}, {}
    )

    # parent_id is configured but absent, so it is left out rather than None.
    assert context == {"name": "reports"}


def test_a_non_dict_body_yields_no_context(folder_logger):
    # Bodies arrive as strings when they are not JSON objects; indexing one
    # would raise, so the extractor has to skip it.
    _, context = folder_logger._extract_business_event(
        {"method": "POST", "path": "/folders", "body": "not-json"}, {}
    )

    assert context == {}


def test_an_empty_body_yields_no_context(folder_logger):
    _, context = folder_logger._extract_business_event(
        {"method": "POST", "path": "/folders", "body": {}}, {}
    )

    assert context == {}
