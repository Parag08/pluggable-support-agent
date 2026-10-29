"""Identity from a short-lived JWT that the company's own (already logged-in) site signs.

The widget only carries the token; this adapter does the trusting. It checks the signature
(HS256 shared secret, or RS256 etc. via the company's JWKS URL), `iss`, `aud`, `exp`, `iat`,
a maximum token age, and requires a `jti` so a token can be used once (see `app/auth/flow.py`).
The customer id is read from `IDENTITY_JWT_CUSTOMER_CLAIM` (default `sub`).
"""

from datetime import datetime, timezone

import jwt

from app.config import Settings
from app.ports.identity import CustomerIdentity, IdentityError


class SignedJwtIdentity:
    name = "signed_jwt"

    def __init__(self, settings: Settings):
        self._alg = settings.identity_jwt_algorithm
        self._secret = settings.identity_jwt_secret.get_secret_value()
        self._issuer, self._audience = settings.identity_jwt_issuer, settings.identity_jwt_audience
        self._claim = settings.identity_jwt_customer_claim
        self._max_age = settings.identity_jwt_max_age_seconds
        self._max_minutes = settings.identity_max_session_minutes
        self._sign_in_url = settings.identity_sign_in_url
        self._jwks = jwt.PyJWKClient(settings.identity_jwt_jwks_url) if settings.identity_jwt_jwks_url else None
        if self._alg.startswith("HS") and not self._secret:
            raise RuntimeError("IDENTITY_JWT_ALGORITHM is HS*: set IDENTITY_JWT_SECRET")
        if not self._alg.startswith("HS") and self._jwks is None:
            raise RuntimeError("IDENTITY_JWT_ALGORITHM is not HS*: set IDENTITY_JWT_JWKS_URL")
        if not self._issuer or not self._audience:
            raise RuntimeError("IDENTITY_JWT_ISSUER and IDENTITY_JWT_AUDIENCE are required")

    def verify(self, token: str) -> CustomerIdentity:
        try:
            key = self._secret if self._jwks is None else self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=[self._alg], audience=self._audience, issuer=self._issuer,
                options={"require": ["exp", "iat", "jti", self._claim]},
            )
        except jwt.ExpiredSignatureError:
            raise IdentityError("TOKEN_EXPIRED") from None
        except jwt.InvalidAudienceError:
            raise IdentityError("WRONG_AUDIENCE") from None
        except jwt.InvalidIssuerError:
            raise IdentityError("WRONG_ISSUER") from None
        except jwt.MissingRequiredClaimError as exc:
            raise IdentityError("MISSING_CLAIM", str(exc.claim)) from None
        except jwt.InvalidTokenError as exc:
            raise IdentityError("INVALID_TOKEN", type(exc).__name__) from None
        except jwt.PyJWKClientError:
            raise IdentityError("KEY_LOOKUP_FAILED") from None

        now = datetime.now(timezone.utc).timestamp()
        if now - float(claims["iat"]) > self._max_age:
            raise IdentityError("TOKEN_TOO_OLD")
        customer_id = str(claims[self._claim]).strip()
        if not customer_id:
            raise IdentityError("MISSING_CLAIM", self._claim)
        return CustomerIdentity(
            customer_id=customer_id, subject=str(claims.get("sub", customer_id)),
            expires_at=datetime.fromtimestamp(float(claims["exp"]), timezone.utc), token_id=str(claims["jti"]),
        )

    def sign_in_url(self, session_id: str) -> str | None:
        return self._sign_in_url.format(session_id=session_id) if self._sign_in_url else None
