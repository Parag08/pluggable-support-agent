"""Identity writes for the auth module (OIDC callback) — deliberately NOT on the agent gateway.

Only code that has validated an identity-server token server-side may call this.
The agent can read identity and expire it, but can never grant it.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import AuthEvent, AuthEventType, ChatSession, IdentityStatus


def mark_identity_validated(
    db: Session, session_id: str, customer_id: str, validated_at: datetime, expires_at: datetime
) -> None:
    session = db.get(ChatSession, session_id)
    session.identity_status = IdentityStatus.VALIDATED
    session.customer_id = customer_id
    session.identity_validated_at = validated_at
    session.identity_expires_at = expires_at
    session.last_activity_at = validated_at
    db.add(AuthEvent(session_id=session.id, event_type=AuthEventType.TOKEN_VALIDATED, customer_id=customer_id))
    db.flush()
