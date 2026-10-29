"""Every CommercePort adapter runs the same suite. To add one, add it to `ADAPTERS`."""

from types import SimpleNamespace

import httpx
import pytest

from app.adapters.commerce.http_json import HttpJsonCommerce
from app.adapters.commerce.http_json_server import as_transport
from app.adapters.commerce.sql_reference import SqlReferenceCommerce
from app.adapters.conversation.sql_store import SqlConversationStore

ADAPTERS = ["sql_reference", "http_json"]


@pytest.fixture(params=ADAPTERS)
def env(request, seeded_db, settings):
    """`commerce` is the adapter under test; `reference` and `db` are the data behind it."""
    reference = SqlReferenceCommerce(seeded_db, settings)
    if request.param == "sql_reference":
        commerce = reference
    else:
        client = httpx.Client(base_url="http://company.test/", transport=as_transport(reference))
        commerce = HttpJsonCommerce(settings, client=client)
    store = SqlConversationStore(seeded_db)
    session = store.create_session("test", app_version="t", prompt_version="t", policy_config_version="t")
    return SimpleNamespace(commerce=commerce, reference=reference, db=seeded_db, session_id=session.id,
                           kind=request.param)
