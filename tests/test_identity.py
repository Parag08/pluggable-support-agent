"""IdentityPort: the signed-JWT adapter and the sign-in flow that grants a session's identity."""

import time
import uuid
from datetime import timedelta

import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.adapters.conversation.sql_store import SqlConversationStore
from app.adapters.identity import build_identity
from app.auth.flow import sign_in_with_token
from app.core.enums import AuthEventType, IdentityStatus
from app.db.base import utcnow
from app.db.models import AuthEvent
from app.ports.identity import IdentityError

SECRET = "test-secret-" * 6
ISS, AUD = "https://shop.example", "support-agent"


@pytest.fixture
def jwt_settings(settings):
    return settings.model_copy(update={
        "identity_provider": "signed_jwt", "identity_jwt_algorithm": "HS256", "identity_jwt_secret": SecretStr(SECRET),
        "identity_jwt_jwks_url": "", "identity_jwt_issuer": ISS, "identity_jwt_audience": AUD,
        "identity_jwt_customer_claim": "sub", "identity_jwt_max_age_seconds": 300,
        "identity_sign_in_url": "https://shop.example/login?chat={session_id}",
    })


@pytest.fixture
def identity(jwt_settings):
    return build_identity(jwt_settings)


def token(secret=SECRET, algorithm="HS256", **override):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "cus_9001", "iat": now, "exp": now + 300, "jti": str(uuid.uuid4())}
    claims.update(override)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm=algorithm)


def test_valid_token_is_verified(identity):
    who = identity.verify(token())
    assert who.customer_id == "cus_9001" and who.token_id and who.expires_at > utcnow()


@pytest.mark.parametrize("bad, code", [
    (lambda: token(exp=int(time.time()) - 10, iat=int(time.time()) - 400), "TOKEN_EXPIRED"),
    (lambda: token(aud="someone-else"), "WRONG_AUDIENCE"),
    (lambda: token(iss="https://evil.example"), "WRONG_ISSUER"),
    (lambda: token(secret="another-secret-another-secret-32bytes!"), "INVALID_TOKEN"),
    (lambda: token(algorithm="HS384"), "INVALID_TOKEN"),
    (lambda: token(jti=None), "MISSING_CLAIM"),
    (lambda: token(sub=None), "MISSING_CLAIM"),
    (lambda: token(iat=int(time.time()) - 1000), "TOKEN_TOO_OLD"),
    (lambda: "not-a-jwt", "INVALID_TOKEN"),
])
def test_bad_tokens_are_rejected_with_a_stable_code(identity, bad, code):
    with pytest.raises(IdentityError) as exc:
        identity.verify(bad())
    assert exc.value.code == code


def test_sign_in_url_is_filled_in(identity):
    assert identity.sign_in_url("abc") == "https://shop.example/login?chat=abc"


def test_adapter_refuses_to_start_misconfigured(jwt_settings):
    with pytest.raises(RuntimeError, match="IDENTITY_JWT_SECRET"):
        build_identity(jwt_settings.model_copy(update={"identity_jwt_secret": SecretStr("")}))
    with pytest.raises(RuntimeError, match="ISSUER"):
        build_identity(jwt_settings.model_copy(update={"identity_jwt_issuer": ""}))


# --- the flow that actually signs a session in ---------------------------------------


@pytest.fixture
def chat(seeded_db):
    store = SqlConversationStore(seeded_db)
    return store, store.create_session("web", app_version="t", prompt_version="t", policy_config_version="t")


def sign_in(db, identity, session, tok, **kw):
    return sign_in_with_token(db, identity, session.id, tok, now=utcnow(), max_session_minutes=kw.get("minutes", 60))


def test_flow_signs_the_session_in(seeded_db, identity, chat):
    store, session = chat
    sign_in(seeded_db, identity, session, token())
    after = store.get_session(session.id)
    assert after.identity_status == IdentityStatus.VALIDATED and after.customer_id == "cus_9001"


def test_session_identity_is_capped_by_max_session_minutes(seeded_db, identity, chat):
    store, session = chat
    sign_in(seeded_db, identity, session, token(exp=int(time.time()) + 3600), minutes=10)
    assert store.get_session(session.id).identity_expires_at <= utcnow() + timedelta(minutes=10, seconds=5)


def test_a_token_can_be_used_only_once(seeded_db, identity, chat):
    store, session = chat
    tok = token()
    sign_in(seeded_db, identity, session, tok)
    other = store.create_session("web", app_version="t", prompt_version="t", policy_config_version="t")
    with pytest.raises(IdentityError) as exc:
        sign_in(seeded_db, identity, other, tok)  # replayed in another chat
    assert exc.value.code == "TOKEN_REPLAYED"
    assert store.get_session(other.id).customer_id is None


def test_failed_sign_in_grants_nothing_and_is_audited_without_the_token(seeded_db, identity, chat):
    store, session = chat
    bad = token(aud="wrong")
    with pytest.raises(IdentityError):
        sign_in(seeded_db, identity, session, bad)
    assert store.get_session(session.id).customer_id is None
    events = seeded_db.scalars(select(AuthEvent).where(AuthEvent.session_id == session.id)).all()
    assert [(e.event_type, e.reason_code) for e in events] == [(AuthEventType.VALIDATION_FAILED, "WRONG_AUDIENCE")]
    assert all(bad not in str(e.details) for e in events)


def test_unknown_session_is_rejected(seeded_db, identity):
    with pytest.raises(IdentityError) as exc:
        sign_in_with_token(seeded_db, identity, "no-such-session", token(), now=utcnow(), max_session_minutes=60)
    assert exc.value.code == "UNKNOWN_SESSION"


def test_customers_that_only_exist_in_the_company_system_can_sign_in(seeded_db, identity, chat):
    """The module's database knows nothing about the company's customers (any id format is fine)."""
    store, session = chat
    who = sign_in(seeded_db, identity, session, token(sub="shopify|gid://customer/8812734"))
    seeded_db.commit()
    assert store.get_session(session.id).customer_id == who.customer_id == "shopify|gid://customer/8812734"
    store.expire_identity(session.id, "TEST")  # also audited without a customers row
    seeded_db.commit()
