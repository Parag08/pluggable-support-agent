"""Conversation logs and pipeline traces used to debug, evaluate, and improve the agent.

Shape of the data:

    chat_sessions ─┬─ messages            what the customer and agent said (PII-redacted)
                   ├─ turns ─┬─ pipeline_steps   input/output/latency of every stage
                   │         └─ llm_calls        model, params, tokens, cost, payloads
                   ├─ parked_intents      protected intents waiting on identity
                   ├─ auth_events         identity-server flow (never tokens)
                   ├─ escalations         human handoffs + handoff quality
                   ├─ message_feedback    thumbs up/down
                   └─ evaluations         evaluator-model grades + human calibration labels

`turns` duplicates the most-queried fields from its steps (intent, confidence,
policy outcome, ...) so dashboards and eval queries don't need to parse JSON.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, UTCDateTime, new_uuid, str_enum, utcnow


from app.core.enums import (
    Gender,
    Width,
    FitNote,
    OrderStatus,
    OrderItemStatus,
    ReturnType,
    ReturnStatus,
    ReturnReason,
    RefundStatus,
    ActorType,
    IdentityStatus,
    SessionStatus,
    MessageRole,
    IntentType,
    PipelineStage,
    StepStatus,
    PolicyOutcome,
    TurnOutcome,
    LLMPurpose,
    ParkedIntentStatus,
    ParkReason,
    AuthEventType,
    EscalationReason,
    EscalationPriority,
    EscalationStatus,
    FeedbackRating,
    EvalTarget,
    EvalType,
)  # noqa: F401  (re-exported from app.db.models)


# --- Sessions & transcript --------------------------------------------------


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[SessionStatus] = mapped_column(str_enum(SessionStatus), default=SessionStatus.ACTIVE, index=True)

    channel: Mapped[str] = mapped_column(String(20))
    locale: Mapped[str | None] = mapped_column(String(10))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    client_ip_hash: Mapped[str | None] = mapped_column(String(64))  # hashed, never raw IP
    page_url: Mapped[str | None] = mapped_column(String(500))  # where the widget was opened

    # Identity (set only by the auth module after server-side token validation)
    identity_status: Mapped[IdentityStatus] = mapped_column(
        str_enum(IdentityStatus), default=IdentityStatus.ANONYMOUS
    )
    # The company's own customer id: deliberately NOT a foreign key, because the company's customers
    # live in its own systems (any id format), not in this module's database.
    customer_id: Mapped[str | None] = mapped_column(String(128), index=True)
    identity_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    identity_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Slot-filling state: active intent, collected fields, queued intents.
    state: Mapped[dict] = mapped_column(JSONType, default=dict)

    # Versions of everything that shapes behaviour — needed to compare runs / roll back.
    app_version: Mapped[str | None] = mapped_column(String(40))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    policy_config_version: Mapped[str | None] = mapped_column(String(40))
    experiment_variant: Mapped[str | None] = mapped_column(String(40))

    csat_score: Mapped[int | None] = mapped_column(SmallInteger)  # 1–5
    csat_comment: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)

    messages: Mapped[list["Message"]] = relationship(back_populates="session", order_by="Message.seq")
    turns: Mapped[list["Turn"]] = relationship(back_populates="session", order_by="Turn.seq")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)  # PII-redacted text
    # Structured UI payload: quick replies, order card, confirmation card, sign-in button…
    payload: Mapped[dict | None] = mapped_column(JSONType)
    # Entity types redacted from this message (e.g. ["card_number"]) — never the values.
    redactions: Mapped[list] = mapped_column(JSONType, default=list)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id", use_alter=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


# --- Pipeline traces --------------------------------------------------------


class Turn(Base):
    """One pass of the pipeline for one user message (or a resumed parked intent)."""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        Index("ix_turns_intent_outcome", "intent", "outcome"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    user_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    assistant_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    resumed_parked_intent_id: Mapped[int | None] = mapped_column(ForeignKey("parked_intents.id", use_alter=True))

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Denormalised from steps for fast analysis.
    intent: Mapped[str | None] = mapped_column(String(40))
    intent_type: Mapped[IntentType | None] = mapped_column(str_enum(IntentType))
    classifier_confidence: Mapped[float | None] = mapped_column(Float)
    sentiment: Mapped[str | None] = mapped_column(String(20))
    entities: Mapped[dict | None] = mapped_column(JSONType)
    missing_fields: Mapped[list | None] = mapped_column(JSONType)
    policy_outcome: Mapped[PolicyOutcome | None] = mapped_column(str_enum(PolicyOutcome))
    policy_reason_code: Mapped[str | None] = mapped_column(String(60))
    handler: Mapped[str | None] = mapped_column(String(60))
    output_check_passed: Mapped[bool | None] = mapped_column(Boolean)
    outcome: Mapped[TurnOutcome | None] = mapped_column(str_enum(TurnOutcome))
    error: Mapped[str | None] = mapped_column(Text)

    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)

    session: Mapped[ChatSession] = relationship(back_populates="turns")
    steps: Mapped[list["PipelineStep"]] = relationship(back_populates="turn", order_by="PipelineStep.seq")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="turn")


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"
    __table_args__ = (UniqueConstraint("turn_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    stage: Mapped[PipelineStage] = mapped_column(str_enum(PipelineStage), index=True)
    status: Mapped[StepStatus] = mapped_column(str_enum(StepStatus))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input: Mapped[dict | None] = mapped_column(JSONType)
    output: Mapped[dict | None] = mapped_column(JSONType)
    # Parameters in effect for this step: thresholds, schema name, policy rule
    # versions, handler name, retrieved KB chunk ids, etc.
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    turn: Mapped[Turn] = relationship(back_populates="steps")


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id"), index=True)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_steps.id"))
    purpose: Mapped[LLMPurpose] = mapped_column(str_enum(LLMPurpose))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80), index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    system_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    # temperature, max_tokens, top_p, output schema name, etc.
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    request_messages: Mapped[list | None] = mapped_column(JSONType)  # redacted
    response_content: Mapped[dict | list | None] = mapped_column(JSONType)
    stop_reason: Mapped[str | None] = mapped_column(String(40))

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    retries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    turn: Mapped[Turn | None] = relationship(back_populates="llm_calls")


# --- Identity, handoff, feedback, evaluation -------------------------------


class ParkedIntent(Base):
    __tablename__ = "parked_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    parked_by_turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"))
    intent: Mapped[str] = mapped_column(String(40))
    entities: Mapped[dict] = mapped_column(JSONType)
    reason: Mapped[ParkReason] = mapped_column(str_enum(ParkReason))
    status: Mapped[ParkedIntentStatus] = mapped_column(str_enum(ParkedIntentStatus))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuthEvent(Base):
    """Identity-flow audit trail (PRD ID-11). Must never contain tokens or codes."""

    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    event_type: Mapped[AuthEventType] = mapped_column(str_enum(AuthEventType))
    reason_code: Mapped[str | None] = mapped_column(String(60))
    customer_id: Mapped[str | None] = mapped_column(String(128))  # the company's id; not a foreign key
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id"))
    reason: Mapped[EscalationReason] = mapped_column(str_enum(EscalationReason), index=True)
    priority: Mapped[EscalationPriority] = mapped_column(str_enum(EscalationPriority))
    intent: Mapped[str | None] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    helpdesk_ticket_id: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[EscalationStatus] = mapped_column(str_enum(EscalationStatus))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    picked_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Filled by the human agent — feeds the "escalation quality" KPI.
    was_appropriate: Mapped[bool | None] = mapped_column(Boolean)
    human_had_to_reask: Mapped[bool | None] = mapped_column(Boolean)


class MessageFeedback(Base):
    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    rating: Mapped[FeedbackRating] = mapped_column(str_enum(FeedbackRating))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Evaluation(Base):
    """A grade from the evaluator agent (any LLM provider) or a human reviewer."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), index=True)  # CI run / batch id
    target_type: Mapped[EvalTarget] = mapped_column(str_enum(EvalTarget))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    eval_type: Mapped[EvalType] = mapped_column(str_enum(EvalType), index=True)
    evaluator: Mapped[str] = mapped_column(String(60))  # "<provider>:<model>", "human:<reviewer>"
    evaluator_model: Mapped[str | None] = mapped_column(String(80))
    rubric_version: Mapped[str] = mapped_column(String(40))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    scores: Mapped[dict] = mapped_column(JSONType, default=dict)  # criterion → score
    violated_rules: Mapped[list] = mapped_column(JSONType, default=list)  # PRD §8 rule ids
    unsupported_claims: Mapped[list] = mapped_column(JSONType, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    # Calibration: human label for the same item, and whether it agrees with the judge.
    human_label: Mapped[dict | None] = mapped_column(JSONType)
    human_agrees: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UsedToken(Base):
    """Single-use identity tokens (JWT `jti`): a replayed token is rejected."""

    __tablename__ = "used_tokens"

    token_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"))
    used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
