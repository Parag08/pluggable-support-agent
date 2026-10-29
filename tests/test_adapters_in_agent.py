"""The agent working through its ports: capability gating, safe write retries, handoff, and a
classifier that is not an LLM. (Adapter behaviour itself is covered in tests/conformance/.)"""

import smtplib

import pytest
from sqlalchemy import func, select

from app.adapters.classifier import build_classifier
from app.adapters.commerce.sql_reference import SqlReferenceCommerce
from app.adapters.handoff.email import EmailHandoff, build_message
from app.agent.contracts import ClassifierOutput
from app.core.types import ChatLine
from app.db.models import Escalation, EscalationReason, LLMCall, ReturnRequest, Turn, TurnOutcome
from app.ports.classifier import ClassifierRun
from app.ports.commerce import ALL_CAPABILITIES, EXCHANGES_WRITE
from app.ports.handoff import HandoffTicket, TicketRef
from app.wiring import build_agent
from tests.agent_helpers import FakeLLM, classification, reply, sign_in


class Wrapped:
    """A CommercePort that delegates to another and lets a test change one behaviour."""

    def __init__(self, inner, capabilities=None):
        self._inner, self._caps = inner, capabilities

    def capabilities(self):
        return self._caps if self._caps is not None else self._inner.capabilities()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FlakyReturns(Wrapped):
    """create_return fails `times` times; `after_write` means the write happened but the answer was lost."""

    def __init__(self, inner, times, after_write):
        super().__init__(inner)
        self.times, self.after_write, self.calls = times, after_write, 0

    def create_return(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.times:
            if self.after_write:
                self._inner.create_return(*args, **kwargs)
            raise ConnectionError("connection lost")
        return self._inner.create_return(*args, **kwargs)


@pytest.fixture
def llm():
    return FakeLLM()


def make(seeded_db, settings, llm, **ports):
    return build_agent(seeded_db, settings, llm=llm, **ports)


def start_return(agent, llm, seeded_db):
    chat = agent.start_session("web")
    sign_in(seeded_db, chat, "cus_9001")
    llm.push(classification("return_request", entities={"order_id": "SS90001", "return_reason": "too_small"}))
    card = agent.handle_message(chat.id, "return the shoes from SS90001, too small")
    assert card.outcome == TurnOutcome.CONFIRM_PENDING
    return chat, card.payload["action_id"]


def return_count(db):
    return db.scalar(select(func.count()).select_from(ReturnRequest).where(ReturnRequest.order_id == "SS90001"))


# --- capabilities -------------------------------------------------------------------


def test_intent_the_company_system_cannot_do_goes_to_a_person_without_asking_anything(seeded_db, settings, llm):
    commerce = Wrapped(SqlReferenceCommerce(seeded_db, settings), ALL_CAPABILITIES - {EXCHANGES_WRITE})
    agent = make(seeded_db, settings, llm, commerce=commerce)
    chat = agent.start_session("web")
    sign_in(seeded_db, chat, "cus_9001")

    llm.push(classification("exchange_request", entities={"order_id": "SS90001"}))
    r = agent.handle_message(chat.id, "swap the shoes from SS90001 for a size 9")

    assert r.outcome == TurnOutcome.ESCALATED
    turn = seeded_db.get(Turn, r.turn_id)
    assert turn.policy_reason_code == "CAPABILITY_UNAVAILABLE"
    esc = seeded_db.scalars(select(Escalation).where(Escalation.turn_id == r.turn_id)).one()
    assert esc.reason == EscalationReason.POLICY_EXCEPTION


def test_supported_intents_keep_working_when_another_is_switched_off(seeded_db, settings, llm):
    commerce = Wrapped(SqlReferenceCommerce(seeded_db, settings), ALL_CAPABILITIES - {EXCHANGES_WRITE})
    agent = make(seeded_db, settings, llm, commerce=commerce)
    chat = agent.start_session("web")
    sign_in(seeded_db, chat, "cus_9001")
    llm.push(classification("track_order", entities={"order_id": "SS90004"}))
    llm.push(reply("Your order SS90004 is shipped."))
    assert agent.handle_message(chat.id, "where is SS90004").outcome == TurnOutcome.ANSWERED


# --- writes: idempotent, retried, never duplicated ------------------------------------


def test_write_that_succeeded_but_lost_its_answer_is_completed_by_retrying_with_the_same_key(
        seeded_db, settings, llm):
    commerce = FlakyReturns(SqlReferenceCommerce(seeded_db, settings), times=1, after_write=True)
    agent = make(seeded_db, settings, llm, commerce=commerce)
    chat, action = start_return(agent, llm, seeded_db)

    done = agent.confirm_action(chat.id, action, accept=True)

    assert done.outcome == TurnOutcome.ANSWERED and done.payload["return_id"]
    assert commerce.calls == 2 and return_count(seeded_db) == 1  # retried, and still exactly one return


def test_outage_before_the_write_is_retried_then_escalated_with_nothing_written(seeded_db, settings, llm):
    commerce = FlakyReturns(SqlReferenceCommerce(seeded_db, settings), times=99, after_write=False)
    agent = make(seeded_db, settings, llm, commerce=commerce)
    chat, action = start_return(agent, llm, seeded_db)

    done = agent.confirm_action(chat.id, action, accept=True)

    assert done.outcome == TurnOutcome.ESCALATED and return_count(seeded_db) == 0
    assert commerce.calls == settings.write_retries + 1
    esc = seeded_db.scalars(select(Escalation).where(Escalation.turn_id == done.turn_id)).one()
    assert f"idempotency key {action}" in esc.summary  # the human can check the company's system


def test_a_refused_write_is_not_retried(seeded_db, settings, llm):
    commerce = SqlReferenceCommerce(seeded_db, settings)
    agent = make(seeded_db, settings, llm, commerce=commerce)
    chat, action = start_return(agent, llm, seeded_db)
    # Something else returns the item between the card and the click.
    item = commerce.get_customer_order("cus_9001", "SS90001").items[0].id
    from decimal import Decimal
    from app.db.base import utcnow
    commerce.create_return("cus_9001", "SS90001", item, "too_small", Decimal("1"), idempotency_key="other",
                           chat_session_id=chat.id, now=utcnow())

    done = agent.confirm_action(chat.id, action, accept=True)

    assert done.outcome != TurnOutcome.ANSWERED and return_count(seeded_db) == 1


# --- handoff ---------------------------------------------------------------------------


class RecordingHandoff:
    name = "recording"

    def __init__(self, fail=False):
        self.tickets, self.fail = [], fail

    def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        if self.fail:
            raise RuntimeError("helpdesk is down")
        self.tickets.append(ticket)
        return TicketRef(external_id=f"T-{len(self.tickets)}")


def ask_for_a_person(agent, llm, seeded_db, signed_in):
    chat = agent.start_session("web")
    if signed_in:
        sign_in(seeded_db, chat, "cus_9001")
    llm.push(classification("human_handoff"))
    return agent.handle_message(chat.id, "I want to talk to a person")


def test_escalation_opens_a_ticket_in_the_helpdesk_and_stores_its_id(seeded_db, settings, llm):
    handoff = RecordingHandoff()
    r = ask_for_a_person(make(seeded_db, settings, llm, handoff=handoff), llm, seeded_db, signed_in=True)

    assert r.outcome == TurnOutcome.ESCALATED
    ticket = handoff.tickets[0]
    assert ticket.reason == "customer_request" and ticket.customer_id == "cus_9001"
    assert any("talk to a person" in line.content for line in ticket.transcript)
    esc = seeded_db.scalars(select(Escalation).where(Escalation.turn_id == r.turn_id)).one()
    assert esc.helpdesk_ticket_id == "T-1"


def test_ticket_names_no_customer_when_not_signed_in(seeded_db, settings, llm):
    handoff = RecordingHandoff()
    ask_for_a_person(make(seeded_db, settings, llm, handoff=handoff), llm, seeded_db, signed_in=False)
    assert handoff.tickets[0].customer_id is None


def test_a_helpdesk_outage_never_loses_the_handoff(seeded_db, settings, llm):
    r = ask_for_a_person(make(seeded_db, settings, llm, handoff=RecordingHandoff(fail=True)), llm, seeded_db, True)

    assert r.outcome == TurnOutcome.ESCALATED
    esc = seeded_db.scalars(select(Escalation).where(Escalation.turn_id == r.turn_id)).one()
    assert esc.helpdesk_ticket_id is None and "helpdesk is down" not in r.text
    assert "HANDOFF_FAILED" in seeded_db.get(Turn, r.turn_id).error


def test_email_handoff_sends_summary_and_transcript(settings, monkeypatch):
    s = settings.model_copy(update={"handoff_email_to": "help@shop.example", "handoff_email_from": "agent@shop.example",
                                    "smtp_host": "smtp.test", "smtp_username": ""})
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == "smtp.test"
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): raise AssertionError("no credentials configured")
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    ticket = HandoffTicket("sess-1", 7, "customer_request", "normal", "human_handoff", "Customer wants a person",
                           (ChatLine("user", "hello, my order is late"),), "cus_9001", ("SS90004",))
    ref = EmailHandoff(s).create_ticket(ticket)

    body = sent[0].get_content()
    assert sent[0]["To"] == "help@shop.example" and ref.external_id == sent[0]["Message-ID"]
    assert "hello, my order is late" in body and "cus_9001" in body and "SS90004" in body


def test_email_handoff_needs_its_settings(settings):
    with pytest.raises(RuntimeError, match="HANDOFF_EMAIL_TO"):
        EmailHandoff(settings.model_copy(update={"handoff_email_to": ""}))


def test_email_subject_shows_priority():
    msg = build_message(HandoffTicket("abcdefghijk", 1, "frustration", "high", None, "s", (), None), "a@x", "b@x")
    assert msg["Subject"].startswith("[HIGH]")


# --- a classifier that is not an LLM ----------------------------------------------------


class KeywordClassifier:
    """A deliberately tiny non-LLM classifier, to prove the port: 'store' in the text → store_info."""

    name = "keywords"

    def __init__(self, settings, config, llm):
        pass

    def classify(self, pre, state, history):
        intent = "store_info" if "store" in pre.text.lower() else "out_of_scope"
        output = ClassifierOutput.model_validate_json(classification(intent, confidence=0.9))
        return ClassifierRun(output=output, model="keyword-rules-v1", params={"rules": 2})


def test_classifier_provider_can_be_swapped_for_a_non_llm_classifier(seeded_db, settings, llm):
    s = settings.model_copy(update={"classifier_provider": "tests.test_adapters_in_agent:KeywordClassifier"})
    agent = build_agent(seeded_db, s, llm=llm)
    assert isinstance(agent.classifier, KeywordClassifier)
    chat = agent.start_session("web")
    llm.push(reply("We have stores in Chicago and Austin."))  # only the reply is written by the LLM

    r = agent.handle_message(chat.id, "Which stores are near me?")

    assert r.outcome == TurnOutcome.ANSWERED and seeded_db.get(Turn, r.turn_id).intent == "store_info"
    assert llm.calls_for("support_classification") == []
    purposes = {c.purpose.value for c in seeded_db.scalars(select(LLMCall).where(LLMCall.turn_id == r.turn_id))}
    assert purposes == {"respond"}  # no classify call was logged, but the stage is still traced
    classify = next(st for st in seeded_db.get(Turn, r.turn_id).steps if st.stage.value == "classify")
    assert classify.params["model"] == "keyword-rules-v1"


def test_unknown_classifier_provider_is_rejected(settings, llm):
    from app.agent.config_loader import get_agent_config
    with pytest.raises(RuntimeError, match="Unknown CLASSIFIER_PROVIDER"):
        build_classifier(settings.model_copy(update={"classifier_provider": "nonsense"}), get_agent_config(), llm)
