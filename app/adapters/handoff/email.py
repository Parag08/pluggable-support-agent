"""Handoff by email: the summary and transcript go to the company's support address over SMTP."""

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from app.config import Settings
from app.ports.handoff import HandoffTicket, TicketRef


def build_message(ticket: HandoffTicket, sender: str, recipient: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[{ticket.priority.upper()}] Support handoff: {ticket.reason} (session {ticket.session_id[:8]})"
    msg["From"] = sender
    msg["To"] = recipient
    msg["Message-ID"] = make_msgid(domain="support-agent.local")
    transcript = "\n".join(f"{line.role}: {line.content}" for line in ticket.transcript)
    msg.set_content(
        f"Reason: {ticket.reason}\nPriority: {ticket.priority}\nIntent: {ticket.intent or 'unknown'}\n"
        f"Verified customer: {ticket.customer_id or 'no (not signed in)'}\n"
        f"Orders: {', '.join(ticket.order_ids) or '-'}\n\n"
        f"Summary\n-------\n{ticket.summary}\n\nTranscript\n----------\n{transcript}\n"
    )
    return msg


class EmailHandoff:
    name = "email"

    def __init__(self, settings: Settings):
        self._to, self._from = settings.handoff_email_to, settings.handoff_email_from
        self._host, self._port = settings.smtp_host, settings.smtp_port
        self._user, self._password = settings.smtp_username, settings.smtp_password.get_secret_value()
        self._starttls = settings.smtp_starttls
        if not (self._to and self._from and self._host):
            raise RuntimeError("HANDOFF_PROVIDER=email needs HANDOFF_EMAIL_TO, HANDOFF_EMAIL_FROM and SMTP_HOST")

    def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        msg = build_message(ticket, self._from, self._to)
        with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)
        return TicketRef(external_id=msg["Message-ID"])
