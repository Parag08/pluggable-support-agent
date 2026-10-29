"""Granting a session's identity. Used only by the auth/server layer, never by the agent.

    verify (IdentityPort) → reject replayed tokens → mark the session validated → audit

Every success and failure is written to `auth_events` (reason codes only, never the token).
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.auth.grant import mark_identity_validated
from app.core.enums import AuthEventType
from app.db.models import AuthEvent, ChatSession, UsedToken
from app.ports.identity import CustomerIdentity, IdentityError, IdentityPort


def _fail(db: Session, session_id: str, code: str) -> IdentityError:
    db.add(AuthEvent(session_id=session_id, event_type=AuthEventType.VALIDATION_FAILED, reason_code=code))
    db.flush()
    return IdentityError(code)


def sign_in_with_token(
    db: Session, identity: IdentityPort, session_id: str, token: str, *, now: datetime, max_session_minutes: int,
) -> CustomerIdentity:
    """Verify the token and sign the session in. Raises IdentityError (after auditing it) if not.

    The session's identity lasts until the token expires or `max_session_minutes`, whichever is sooner.
    """
    if db.get(ChatSession, session_id) is None:
        raise IdentityError("UNKNOWN_SESSION")
    try:
        who = identity.verify(token)
    except IdentityError as exc:
        raise _fail(db, session_id, exc.code) from None
    if who.expires_at <= now:
        raise _fail(db, session_id, "TOKEN_EXPIRED")
    if who.token_id is not None:
        if db.get(UsedToken, who.token_id) is not None:
            raise _fail(db, session_id, "TOKEN_REPLAYED")
        db.add(UsedToken(token_id=who.token_id, session_id=session_id, used_at=now, expires_at=who.expires_at))
    mark_identity_validated(db, session_id, who.customer_id, now,
                            min(who.expires_at, now + timedelta(minutes=max_session_minutes)))
    return who
