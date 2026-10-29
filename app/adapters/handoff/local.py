"""No external helpdesk: the escalation record in the module's own store is the handoff."""

from app.config import Settings
from app.ports.handoff import HandoffTicket, TicketRef


class LocalHandoff:
    name = "local"

    def __init__(self, settings: Settings | None = None):
        pass

    def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        return TicketRef(external_id=None)
