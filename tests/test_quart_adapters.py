"""
Tests for the Quart adapters' user ID and response body extraction: where
each looks, in what order, and what it returns for each kind of response.
"""

import json
from types import SimpleNamespace

import pytest
from quart import Quart, Response, g

from auditry.quart.adapters import QuartRequestAdapter, QuartResponseAdapter

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app():
    return Quart(__name__)


async def user_id_of(app, request, **g_attributes):
    async with app.app_context():
        for name, value in g_attributes.items():
            setattr(g, name, value)
        return await QuartRequestAdapter().extract_user_id(request)


async def body_of(response):
    return await QuartResponseAdapter().extract_body(response)


async def test_user_id_from_the_current_users_id(app):
    request = SimpleNamespace(current_user=SimpleNamespace(id=7, user_id="u-9"))
    assert await user_id_of(app, request) == "7"


async def test_user_id_from_the_current_users_user_id_without_an_id(app):
    request = SimpleNamespace(current_user=SimpleNamespace(user_id="u-9"))
    assert await user_id_of(app, request) == "u-9"


async def test_current_user_comes_before_g_user(app):
    request = SimpleNamespace(current_user=SimpleNamespace(id=7))
    assert await user_id_of(app, request, user=SimpleNamespace(id=8)) == "7"


async def test_user_id_from_g_user(app):
    assert await user_id_of(app, SimpleNamespace(), user=SimpleNamespace(id=8)) == "8"


async def test_g_user_without_an_id_falls_through_to_g_user_id(app):
    request = SimpleNamespace()
    assert await user_id_of(app, request, user=SimpleNamespace(id=None), user_id="g-3") == "g-3"


async def test_g_user_id_comes_before_the_requests_user_id(app):
    assert await user_id_of(app, SimpleNamespace(user_id="r-4"), user_id="g-3") == "g-3"


async def test_user_id_from_the_request(app):
    assert await user_id_of(app, SimpleNamespace(user_id="r-4")) == "r-4"


async def test_no_user_id_anywhere(app):
    assert await user_id_of(app, SimpleNamespace(current_user=None)) is None


async def test_body_of_a_response(app):
    async with app.app_context():
        assert await body_of(Response("hello")) == b"hello"


async def test_body_of_a_streaming_response_is_not_read(app):
    async def chunks():
        yield b"chunk"

    async with app.app_context():
        assert await body_of(Response(chunks())) is None


async def test_body_of_a_tuple_with_text(app):
    assert await body_of(("hello", 200)) == b"hello"


async def test_body_of_a_tuple_with_bytes(app):
    assert await body_of((b"raw",)) == b"raw"


async def test_body_of_a_tuple_with_a_dict(app):
    assert await body_of(({"ok": True}, 201)) == json.dumps({"ok": True}).encode()


async def test_body_of_a_tuple_with_another_type(app):
    assert await body_of((42, 200)) is None


async def test_body_of_something_else(app):
    assert await body_of(object()) is None
