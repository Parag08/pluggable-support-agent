"""The HTTP API end to end (fake model, in-process): message → sign-in required → sign in → resumed → confirm."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.server.main import create_app
from tests.agent_helpers import FakeLLM, classification, reply


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def client(seeded_db, settings, llm):
    app = create_app(settings, llm, enable_trace=True, session_factory=sessionmaker(bind=seeded_db.get_bind()))
    return TestClient(app)


def start(client) -> str:
    return client.post("/v1/sessions").json()["session_id"]


def test_sign_in_then_resume_then_return_with_confirmation(client, llm):
    sid = start(client)

    llm.push(classification("track_order", entities={"order_id": "SS90004"}))
    first = client.post(f"/v1/sessions/{sid}/messages", json={"text": "Where is SS90004?"}).json()
    assert first["outcome"] == "auth_required" and first["payload"]["type"] == "sign_in"

    llm.push(reply("Your order SS90004 has shipped."))
    signed = client.post(f"/v1/sessions/{sid}/identity", json={"token": "cus_9001"}).json()  # dev identity
    assert signed["signed_in"] and signed["first_name"] == "Maya"
    assert signed["reply"]["outcome"] == "answered"  # the parked request resumed by itself

    llm.push(classification("return_request", entities={"order_id": "SS90001", "return_reason": "too_small"}))
    card = client.post(f"/v1/sessions/{sid}/messages", json={"text": "return SS90001, too small"}).json()
    assert card["outcome"] == "confirm_pending"
    done = client.post(f"/v1/sessions/{sid}/actions/{card['payload']['action_id']}", json={"accept": True}).json()
    assert done["outcome"] == "answered" and done["payload"]["return_id"]

    trace = client.get(f"/v1/turns/{done['turn_id']}/trace").json()
    assert trace["handler"] == "create_return" and "execute" in [s["stage"] for s in trace["stages"]]


def test_failed_sign_in_is_rejected_and_grants_nothing(client, llm):
    sid = start(client)
    assert client.post(f"/v1/sessions/{sid}/identity", json={"token": "  "}).status_code == 401
    llm.push(classification("track_order", entities={"order_id": "SS90004"}))
    still = client.post(f"/v1/sessions/{sid}/messages", json={"text": "Where is SS90004?"}).json()
    assert still["outcome"] == "auth_required"


def test_unknown_session_is_404_and_trace_is_off_by_default(seeded_db, settings, llm):
    app = create_app(settings, llm, session_factory=sessionmaker(bind=seeded_db.get_bind()))
    c = TestClient(app)
    assert c.post("/v1/sessions/nope/messages", json={"text": "hi"}).status_code == 404
    assert c.get("/v1/turns/x/trace").status_code == 404
