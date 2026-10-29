"""ConversationStore: the module's own memory (sessions, transcript, traces, parked requests).

This is not the company's data. It holds only what the agent itself produces, and it is the one
place the agent writes to besides the company's systems.
"""

from datetime import datetime
from typing import Any, Protocol

from app.core.enums import EscalationPriority, EscalationReason, MessageRole
from app.core.types import ChatLine, MessageRef, ParkedRequest, SessionInfo


class ConversationStore(Protocol):
    # --- sessions ---
    def create_session(self, channel: str, *, app_version: str, prompt_version: str,
                       policy_config_version: str, **meta: Any) -> SessionInfo: ...
    def get_session(self, session_id: str) -> SessionInfo | None: ...
    def save_session_state(self, session_id: str, state: dict[str, Any]) -> None: ...
    def mark_session_escalated(self, session_id: str) -> None: ...
    def expire_identity(self, session_id: str, reason_code: str) -> None:
        """The agent may only *remove* identity (timeout/expiry), never grant it."""

    # --- transcript ---
    def log_message(self, session_id: str, role: MessageRole, content: str, *, payload: dict | None = None,
                    redactions: list[str] | None = None, turn_id: str | None = None) -> MessageRef: ...
    def recent_messages(self, session_id: str, before_seq: int, limit: int) -> list[ChatLine]: ...

    # --- parked requests (waiting for sign-in) ---
    def park_intent(self, session_id: str, turn_id: str, intent: str, slots: dict[str, Any], now: datetime) -> int: ...
    def take_parked_intent(self, session_id: str, now: datetime) -> ParkedRequest | None: ...

    # --- escalation records ---
    def create_escalation(self, session_id: str, turn_id: str, reason: EscalationReason,
                          priority: EscalationPriority, intent: str | None, summary: str) -> int: ...
    def set_escalation_ticket(self, escalation_id: int, ticket_id: str | None) -> None: ...

    # --- tracing and transaction boundary ---
    def start_turn(self, session_id: str, user_message_id: int | None) -> Any:
        """Returns a trace handle with `turn_id`, `step()`, `llm_call()` and `finish()`."""
    def commit(self) -> None: ...
