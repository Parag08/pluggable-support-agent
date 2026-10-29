"""HandoffPort: hand a conversation to a human in the company's helpdesk."""

from dataclasses import dataclass, field
from typing import Protocol

from app.core.types import ChatLine


@dataclass(frozen=True)
class HandoffTicket:
    session_id: str
    escalation_id: int
    reason: str
    priority: str
    intent: str | None
    summary: str
    transcript: tuple[ChatLine, ...]
    customer_id: str | None  # only when the customer's identity was verified
    order_ids: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class TicketRef:
    external_id: str | None  # the helpdesk's id for the ticket (None for the local-only adapter)
    url: str | None = None


class HandoffPort(Protocol):
    name: str

    def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        """Create the ticket. May raise; the agent keeps its own escalation record either way."""
