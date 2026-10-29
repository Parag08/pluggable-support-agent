"""Run the Acme Footwear demo storefront with the chat widget.

    python -m demo.run            # then open http://127.0.0.1:8000

Acme's "site" (this file) plays the company: it has its own login, and when a customer signs in
it mints the short-lived signed JWT that the agent's `signed_jwt` identity adapter verifies. The
secret below is random per run and exists only for this demo. Needs LLM_API_KEY etc. in .env.
"""

import os
import secrets
import sys
import time
import uuid

# The demo uses the real signed-JWT identity flow; these override .env for this process only.
SECRET = secrets.token_urlsafe(48)
os.environ.update({
    "IDENTITY_PROVIDER": "signed_jwt", "IDENTITY_JWT_ALGORITHM": "HS256", "IDENTITY_JWT_SECRET": SECRET,
    "IDENTITY_JWT_ISSUER": "https://acme.example", "IDENTITY_JWT_AUDIENCE": "acme-support-agent",
    "IDENTITY_JWT_CUSTOMER_CLAIM": "sub", "IDENTITY_JWT_MAX_AGE_SECONDS": "300",
})

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pathlib import Path  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import get_sessionmaker  # noqa: E402
from app.server.main import create_app  # noqa: E402
from app.wiring import build_agent  # noqa: E402

# The two demo accounts in the seed data.
ACCOUNTS = {"cus_9001": "Maya", "cus_9002": "Daniel"}


class LoginIn(BaseModel):
    customer_id: str


def build_demo_app():
    settings = get_settings()
    app = create_app(settings, enable_trace=True)

    @app.post("/demo/login")
    def login(body: LoginIn) -> dict:
        """Acme's own login: here it just trusts the choice, then signs a token for the chat."""
        if body.customer_id not in ACCOUNTS:
            raise HTTPException(404, "unknown demo account")
        now = int(time.time())
        token = jwt.encode({"iss": "https://acme.example", "aud": "acme-support-agent", "sub": body.customer_id,
                            "iat": now, "exp": now + 300, "jti": str(uuid.uuid4())}, SECRET, algorithm="HS256")
        return {"name": ACCOUNTS[body.customer_id], "customer_id": body.customer_id, "token": token}

    @app.get("/demo/catalog")
    def catalog() -> list[dict]:
        db = get_sessionmaker()()
        try:
            commerce = build_agent(db, settings, llm=_NoLLM()).commerce
            return [{"name": p.name, "category": p.category, "price": str(p.price), "colors": p.colors,
                     "widths": p.widths} for p in commerce.list_products()]
        finally:
            db.close()

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "acme", html=True), name="site")
    return app


class _NoLLM:
    provider = "none"

    def complete(self, **kw):  # the catalog route never calls the model
        raise RuntimeError("not available")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(build_demo_app(), host="127.0.0.1", port=port, log_level="warning")
