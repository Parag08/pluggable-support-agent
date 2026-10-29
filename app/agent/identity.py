"""Read session identity. The agent can expire identity but never grant it
(granting lives in `app.auth`, used only by the auth module)."""

from datetime import datetime, timedelta

from app.config import Settings
from app.core.types import SessionInfo
from app.ports.conversation import ConversationStore
from app.db.models import IdentityStatus


def identity_is_valid(store: ConversationStore, session: SessionInfo, now: datetime, settings: Settings) -> bool:
    if session.identity_status != IdentityStatus.VALIDATED or not session.customer_id:
        return False
    token_expired = session.identity_expires_at is not None and session.identity_expires_at <= now
    idle = session.last_activity_at + timedelta(minutes=settings.identity_idle_timeout_minutes) <= now
    if token_expired or idle:
        store.expire_identity(session.id, "TOKEN_EXPIRED" if token_expired else "IDLE_TIMEOUT")
        return False
    return True
