"""ConversationStore over SQL: sessions, transcript, parked requests, escalation records, traces."""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuthEventType,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    IdentityStatus,
    MessageRole,
    ParkedIntentStatus,
    ParkReason,
    SessionStatus,
)
from app.core.types import ChatLine, MessageRef, ParkedRequest, SessionInfo
from app.db.models import AuthEvent, ChatSession, Escalation, Message, ParkedIntent
from app.telemetry.recorder import TurnRecorder, log_message


def _session(s: ChatSession) -> SessionInfo:
    return SessionInfo(s.id, s.identity_status, s.customer_id, s.identity_expires_at,
                       s.last_activity_at, dict(s.state or {}))


class SqlConversationStore:
    def __init__(self, db: Session):
        self.__db = db  # name-mangled: not part of the agent's surface

    def create_session(self, channel: str, *, app_version: str, prompt_version: str,
                       policy_config_version: str, **meta: Any) -> SessionInfo:
        allowed_meta = {k: meta[k] for k in ("locale", "user_agent", "client_ip_hash", "page_url") if k in meta}
        s = ChatSession(channel=channel, app_version=app_version, prompt_version=prompt_version,
                        policy_config_version=policy_config_version, state={}, **allowed_meta)
        self.__db.add(s)
        self.__db.flush()
        return _session(s)

    def get_session(self, session_id: str) -> SessionInfo | None:
        s = self.__db.get(ChatSession, session_id)
        return _session(s) if s is not None else None

    def save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self.__db.get(ChatSession, session_id).state = dict(state)

    def mark_session_escalated(self, session_id: str) -> None:
        self.__db.get(ChatSession, session_id).status = SessionStatus.ESCALATED

    def expire_identity(self, session_id: str, reason_code: str) -> None:
        """The agent may only *remove* identity (timeout/expiry), never grant it."""
        s = self.__db.get(ChatSession, session_id)
        self.__db.add(AuthEvent(session_id=s.id, event_type=AuthEventType.EXPIRED,
                                customer_id=s.customer_id, reason_code=reason_code))
        s.identity_status = IdentityStatus.EXPIRED
        s.customer_id = None
        self.__db.flush()

    def log_message(self, session_id: str, role: MessageRole, content: str, *, payload: dict | None = None,
                    redactions: list[str] | None = None, turn_id: str | None = None) -> MessageRef:
        msg = log_message(self.__db, session_id, role, content, payload=payload, redactions=redactions,
                          turn_id=turn_id)
        return MessageRef(msg.id, msg.seq)

    def recent_messages(self, session_id: str, before_seq: int, limit: int) -> list[ChatLine]:
        rows = self.__db.scalars(
            select(Message)
            .where(Message.session_id == session_id, Message.seq < before_seq,
                   Message.role.in_([MessageRole.USER, MessageRole.ASSISTANT]))
            .order_by(Message.seq.desc())
            .limit(limit)
        ).all()
        return [ChatLine(m.role.value, m.content) for m in reversed(rows)]

    def park_intent(self, session_id: str, turn_id: str, intent: str, slots: dict[str, Any], now: datetime) -> int:
        for old in self.__db.scalars(select(ParkedIntent).where(
                ParkedIntent.session_id == session_id, ParkedIntent.status == ParkedIntentStatus.PARKED)):
            old.status = ParkedIntentStatus.CANCELLED
            old.resolved_at = now
        parked = ParkedIntent(session_id=session_id, parked_by_turn_id=turn_id, intent=intent, entities=dict(slots),
                              reason=ParkReason.AUTH_REQUIRED, status=ParkedIntentStatus.PARKED)
        self.__db.add(parked)
        self.__db.flush()
        return parked.id

    def take_parked_intent(self, session_id: str, now: datetime) -> ParkedRequest | None:
        """Return the latest request waiting on sign-in and mark it resumed."""
        parked = self.__db.scalars(
            select(ParkedIntent)
            .where(ParkedIntent.session_id == session_id, ParkedIntent.status == ParkedIntentStatus.PARKED,
                   ParkedIntent.reason == ParkReason.AUTH_REQUIRED)
            .order_by(ParkedIntent.id.desc())
        ).first()
        if parked is None:
            return None
        parked.status = ParkedIntentStatus.RESUMED
        parked.resolved_at = now
        return ParkedRequest(parked.id, parked.intent, dict(parked.entities))

    def create_escalation(self, session_id: str, turn_id: str, reason: EscalationReason,
                          priority: EscalationPriority, intent: str | None, summary: str) -> int:
        esc = Escalation(session_id=session_id, turn_id=turn_id, reason=reason, priority=priority,
                         intent=intent, summary=summary, status=EscalationStatus.OPEN)
        self.__db.add(esc)
        self.__db.flush()
        return esc.id

    def set_escalation_ticket(self, escalation_id: int, ticket_id: str | None) -> None:
        self.__db.get(Escalation, escalation_id).helpdesk_ticket_id = ticket_id

    def start_turn(self, session_id: str, user_message_id: int | None) -> TurnRecorder:
        return TurnRecorder(self.__db, session_id, user_message_id)

    def commit(self) -> None:
        self.__db.commit()
