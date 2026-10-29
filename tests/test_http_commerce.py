"""The HTTP/JSON adapter's wire behaviour (the shared behaviour is in tests/conformance/)."""

import httpx
import pytest

from app.adapters.commerce.http_json import CommerceUnavailable, HttpJsonCommerce
from app.core.types import DataAccessError


def adapter(settings, handler):
    client = httpx.Client(base_url="http://company.test/", transport=httpx.MockTransport(handler))
    return HttpJsonCommerce(settings, client=client)


def test_refusals_map_to_data_access_error(settings):
    c = adapter(settings, lambda req: httpx.Response(409, json={"error": "item is not returnable"}))
    with pytest.raises(DataAccessError, match="not returnable"):
        c.get_product("p")


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="not json"),
                                      httpx.Response(200, json={"no": "result"})])
def test_unusable_answers_are_unavailable_not_refusals(settings, response):
    c = adapter(settings, lambda req: response)
    with pytest.raises(CommerceUnavailable):
        c.get_product("p")


def test_network_errors_are_unavailable(settings):
    def boom(req):
        raise httpx.ConnectError("down")
    with pytest.raises(CommerceUnavailable):
        adapter(settings, boom).get_product("p")


def test_writes_send_the_idempotency_key_as_a_header(settings):
    from datetime import datetime, timezone
    from decimal import Decimal

    seen = {}

    def handler(req):
        seen["header"] = req.headers.get("Idempotency-Key")
        seen["path"] = req.url.path
        return httpx.Response(200, json={"result": {"return_id": "R1", "label_url": "u"}})

    adapter(settings, handler).create_return(
        "c", "o", "i", "too_small", Decimal("1"), idempotency_key="key-123", chat_session_id="s",
        now=datetime.now(timezone.utc))
    assert seen == {"header": "key-123", "path": "/create_return"}


def test_capabilities_are_fetched_once(settings):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"result": ["orders.read"]})

    c = adapter(settings, handler)
    assert c.capabilities() == frozenset({"orders.read"}) == c.capabilities()
    assert calls == ["/capabilities"]
