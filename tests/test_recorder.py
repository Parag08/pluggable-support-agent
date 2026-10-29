from decimal import Decimal

import pytest

from app.db.models import (
    ChatSession,
    IntentType,
    LLMPurpose,
    MessageRole,
    PipelineStage,
    PolicyOutcome,
    StepStatus,
    TurnOutcome,
)
from app.telemetry.recorder import TurnRecorder, log_message


@pytest.fixture
def chat(db):
    session = ChatSession(channel="web")
    db.add(session)
    db.flush()
    return session


def test_full_turn_is_traced(db, chat):
    user_msg = log_message(db, chat.id, MessageRole.USER, "Where is my order SS90004?")
    rec = TurnRecorder(db, chat.id, user_message_id=user_msg.id)

    with rec.step(PipelineStage.CLASSIFY, input={"text": user_msg.content}, params={"min_confidence": 0.7}) as step:
        rec.llm_call(
            step=step, purpose=LLMPurpose.CLASSIFY, provider="anthropic", model="test-model",
            params={"temperature": 0}, input_tokens=120, output_tokens=30, cost_usd=Decimal("0.0004"),
            request_messages=[{"role": "user", "content": user_msg.content}],
            response_content={"intent": "track_order"},
        )
        step.output = {"intent": "track_order", "confidence": 0.95}

    with rec.step(PipelineStage.POLICY) as step:
        step.status = StepStatus.SHORT_CIRCUIT
        step.output = {"outcome": "auth_required"}

    reply = log_message(db, chat.id, MessageRole.ASSISTANT, "Please sign in to continue.",
                        payload={"type": "sign_in"}, turn_id=rec.turn.id)
    rec.finish(
        TurnOutcome.AUTH_REQUIRED,
        intent="track_order",
        intent_type=IntentType.PROTECTED,
        classifier_confidence=0.95,
        policy_outcome=PolicyOutcome.AUTH_REQUIRED,
        assistant_message_id=reply.id,
    )
    db.commit()
    turn = rec.turn

    assert turn.seq == 1 and turn.latency_ms is not None and turn.completed_at
    assert [s.stage for s in turn.steps] == [PipelineStage.CLASSIFY, PipelineStage.POLICY]
    assert turn.steps[0].params == {"min_confidence": 0.7}
    assert turn.steps[1].status == StepStatus.SHORT_CIRCUIT
    assert turn.total_input_tokens == 120 and turn.total_cost_usd == Decimal("0.0004")
    assert turn.llm_calls[0].step_id == turn.steps[0].id
    assert user_msg.turn_id == turn.id
    assert [m.seq for m in chat.messages] == [1, 2]


def test_failed_step_is_recorded_and_reraised(db, chat):
    rec = TurnRecorder(db, chat.id)
    with pytest.raises(RuntimeError):
        with rec.step(PipelineStage.EXECUTE, input={"handler": "get_order"}):
            raise RuntimeError("OMS timeout")
    step = rec.turn.steps[0]
    assert step.status == StepStatus.ERROR
    assert "OMS timeout" in step.error
    assert step.latency_ms is not None


def test_finish_rejects_unknown_fields(db, chat):
    rec = TurnRecorder(db, chat.id)
    with pytest.raises(ValueError):
        rec.finish(TurnOutcome.ANSWERED, intnet="typo")


def test_turn_seq_increments_per_session(db, chat):
    assert TurnRecorder(db, chat.id).turn.seq == 1
    assert TurnRecorder(db, chat.id).turn.seq == 2
