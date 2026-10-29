"""Development-only identity: the token is simply a customer id. Never use in production."""

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.ports.identity import CustomerIdentity, IdentityError


class DevIdentity:
    name = "dev"

    def __init__(self, settings: Settings):
        self._minutes = settings.identity_max_session_minutes

    def verify(self, token: str) -> CustomerIdentity:
        customer_id = token.strip()
        if not customer_id:
            raise IdentityError("EMPTY_TOKEN")
        return CustomerIdentity(customer_id, customer_id,
                                datetime.now(timezone.utc) + timedelta(minutes=self._minutes))

    def sign_in_url(self, session_id: str) -> str | None:
        return None
