"""Write conversation logs and pipeline traces (data layer).

The agent never imports this module. It gets a `TurnRecorder` from
`ConversationStore.start_turn()` and uses only `turn_id`, `step()`, `llm_call()`
and `finish()`. Steps are exposed as plain `StepHandle` objects; the database
rows stay private to this module.

Content passed here must already be PII-redacted (pipeline stage 0).
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import (
    ChatSession,
    LLMCall,
    LLMPurpose,
    Message,
    MessageRole,
    PipelineStage,
    PipelineStep,
    StepStatus,
    Turn,
    TurnOutcome,
)

# Fields on Turn that finish() may set — guards against typos silently doing nothing.
_TURN_SUMMARY_FIELDS = {
    "intent", "intent_type", "classifier_confidence", "sentiment", "entities", "missing_fields",
    "policy_outcome", "policy_reason_code", "handler", "output_check_passed", "error",
    "assistant_message_id", "resumed_parked_intent_id",
}


@dataclass
class StepHandle:
    """What the pipeline sets while a stage runs. Written to pipeline_steps when the stage ends."""

    stage: PipelineStage
    input: dict | None
    params: dict = field(default_factory=dict)
    output: Any = None
    status: StepStatus = StepStatus.OK
    error: str | None = None
    id: int | None = None


def _next_seq(db: Session, column, session_id: str) -> int:
    current = db.scalar(select(func.max(column)).where(column.table.c.session_id == session_id))
    return (current or 0) + 1


def log_message(
    db: Session,
    session_id: str,
    role: MessageRole,
    content: str,
    *,
    payload: dict | None = None,
    redactions: list[str] | None = None,
    turn_id: str | None = None,
) -> Message:
    msg = Message(
        session_id=session_id,
        seq=_next_seq(db, Message.seq, session_id),
        role=role,
        content=content,
        payload=payload,
        redactions=redactions or [],
        turn_id=turn_id,
    )
    db.add(msg)
    session = db.get(ChatSession, session_id)
    if session is not None:
        session.last_activity_at = utcnow()
    db.flush()
    return msg


class TurnRecorder:
    def __init__(self, db: Session, session_id: str, user_message_id: int | None = None):
        self._db = db
        self._store_payloads = get_settings().telemetry_store_llm_payloads
        self._t0 = time.perf_counter()
        self._step_seq = 0
        self.turn = Turn(
            session_id=session_id,
            seq=_next_seq(db, Turn.seq, session_id),
            user_message_id=user_message_id,
            started_at=utcnow(),
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=Decimal(0),
        )
        db.add(self.turn)
        db.flush()
        if user_message_id is not None:
            db.get(Message, user_message_id).turn_id = self.turn.id

    @property
    def turn_id(self) -> str:
        return self.turn.id

    @contextmanager
    def step(self, stage: PipelineStage, *, input: dict | None = None, params: dict | None = None) -> Iterator[StepHandle]:
        """Time a pipeline stage. Set `output` / `status` on the handle inside the block.

        An exception marks the step as ERROR, records it, and re-raises.
        """
        self._step_seq += 1
        handle = StepHandle(stage=stage, input=input, params=dict(params or {}))
        row = PipelineStep(turn_id=self.turn.id, seq=self._step_seq, stage=stage, status=StepStatus.OK,
                           started_at=utcnow(), input=input, params=dict(handle.params))  # a copy: later changes must show as changes
        self._db.add(row)
        self._db.flush()
        handle.id = row.id
        t0 = time.perf_counter()
        try:
            yield handle
        except Exception as exc:
            handle.status = StepStatus.ERROR
            handle.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row.status = handle.status
            row.output = handle.output
            row.params = dict(handle.params)
            row.error = handle.error
            row.latency_ms = int((time.perf_counter() - t0) * 1000)
            self._db.flush()

    def llm_call(
        self,
        *,
        purpose: LLMPurpose,
        provider: str,
        model: str,
        step: StepHandle | None = None,
        request_messages: list | None = None,
        response_content: Any = None,
        **fields: Any,
    ) -> None:
        call = LLMCall(
            turn_id=self.turn.id,
            step_id=step.id if step else None,
            purpose=purpose,
            provider=provider,
            model=model,
            request_messages=request_messages if self._store_payloads else None,
            response_content=response_content if self._store_payloads else None,
            **fields,
        )
        self._db.add(call)
        self.turn.total_input_tokens += call.input_tokens or 0
        self.turn.total_output_tokens += call.output_tokens or 0
        self.turn.total_cost_usd += call.cost_usd or Decimal(0)
        self._db.flush()

    def finish(self, outcome: TurnOutcome, **summary: Any) -> None:
        unknown = set(summary) - _TURN_SUMMARY_FIELDS
        if unknown:
            raise ValueError(f"Unknown turn summary fields: {sorted(unknown)}")
        for key, value in summary.items():
            setattr(self.turn, key, value)
        self.turn.outcome = outcome
        self.turn.completed_at = utcnow()
        self.turn.latency_ms = int((time.perf_counter() - self._t0) * 1000)
        self._db.flush()
