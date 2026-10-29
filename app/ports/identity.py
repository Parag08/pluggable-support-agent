"""IdentityPort: who is this customer?

Only an `IdentityPort.verify()` result can set a session's identity (via `app/auth/flow.py`).
The agent core can expire an identity but can never grant one.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CustomerIdentity:
    customer_id: str  # the id the CommercePort knows this customer by
    subject: str  # the identity provider's id for the customer
    expires_at: datetime
    token_id: str | None = None  # single-use id (JWT `jti`), used for replay protection


class IdentityError(Exception):
    """The token could not be verified. `code` is a stable reason for the audit log (never the token)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class IdentityPort(Protocol):
    name: str

    def verify(self, token: str) -> CustomerIdentity:
        """Return the verified identity or raise IdentityError. Never trust a token without calling this."""

    def sign_in_url(self, session_id: str) -> str | None:
        """Where to send a customer who isn't signed in, if the provider has such a page."""
