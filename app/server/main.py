"""HTTP API for the chat widget (or any client): sessions, messages, confirmations, sign-in.

    POST /v1/sessions                                  start a chat
    POST /v1/sessions/{id}/messages     {text}         send a message
    POST /v1/sessions/{id}/actions/{action_id} {accept} answer a confirmation card
    POST /v1/sessions/{id}/identity     {token}        sign in with the company's token, resumes the parked request
    GET  /v1/turns/{turn_id}/trace                     pipeline trace (only when create_app(enable_trace=True))

A new database session and agent are built per request from the `*_PROVIDER` settings
(`app/wiring.py`); the LLM client is shared. The server holds no state of its own.
"""

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.adapters.llm import build_llm_client
from app.agent.llm import LLMClient
from app.agent.orchestrator import AgentReply, utcnow
from app.auth.flow import sign_in_with_token
from app.config import Settings, get_settings
from app.db.base import get_sessionmaker
from app.db.models import Turn
from app.ports.identity import IdentityError
from app.wiring import build_agent, build_identity


class MessageIn(BaseModel):
    text: str = Field(max_length=10_000)


class ActionIn(BaseModel):
    accept: bool


class IdentityIn(BaseModel):
    token: str = Field(max_length=8_000)


def _reply(r: AgentReply | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"text": r.text, "outcome": r.outcome.value, "turn_id": r.turn_id, "payload": r.payload}


def create_app(settings: Settings | None = None, llm: LLMClient | None = None, *,
               enable_trace: bool = False, session_factory=None) -> FastAPI:
    s = settings or get_settings()
    shared_llm = llm or build_llm_client(s)
    identity = build_identity(s)
    sessions = session_factory or get_sessionmaker()
    app = FastAPI(title="Support agent API", version=s.app_version)

    def with_agent(fn):
        db = sessions()
        try:
            return fn(db, build_agent(db, s, llm=shared_llm, identity=identity))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        finally:
            db.close()

    @app.post("/v1/sessions")
    def start_session() -> dict:
        return with_agent(lambda db, agent: {"session_id": agent.start_session("web").id})

    @app.post("/v1/sessions/{session_id}/messages")
    def send_message(session_id: str, body: MessageIn) -> dict:
        return with_agent(lambda db, agent: _reply(agent.handle_message(session_id, body.text)))

    @app.post("/v1/sessions/{session_id}/actions/{action_id}")
    def answer_card(session_id: str, action_id: str, body: ActionIn) -> dict:
        return with_agent(lambda db, agent: _reply(agent.confirm_action(session_id, action_id, body.accept)))

    @app.post("/v1/sessions/{session_id}/identity")
    def sign_in(session_id: str, body: IdentityIn) -> dict:
        def run(db, agent):
            try:
                who = sign_in_with_token(db, identity, session_id, body.token, now=utcnow(),
                                         max_session_minutes=s.identity_max_session_minutes)
            except IdentityError as exc:
                db.commit()  # keep the audit event
                raise HTTPException(401, {"error": exc.code}) from None
            db.commit()
            profile = agent.commerce.get_customer_profile(who.customer_id)
            return {"signed_in": True, "first_name": profile.first_name if profile else None,
                    "reply": _reply(agent.resume_after_auth(session_id))}
        return with_agent(run)

    if enable_trace:
        @app.get("/v1/turns/{turn_id}/trace")
        def trace(turn_id: str) -> dict:
            db = sessions()
            try:
                turn = db.get(Turn, turn_id)
                if turn is None:
                    raise HTTPException(404, "unknown turn")
                return {
                    "intent": turn.intent, "confidence": turn.classifier_confidence,
                    "sentiment": turn.sentiment, "policy_outcome": turn.policy_outcome.value if turn.policy_outcome else None,
                    "policy_reason": turn.policy_reason_code, "handler": turn.handler,
                    "outcome": turn.outcome.value if turn.outcome else None,
                    "stages": [{"stage": st.stage.value, "status": st.status.value, "ms": st.latency_ms}
                               for st in turn.steps],
                }
            finally:
                db.close()

    return app
