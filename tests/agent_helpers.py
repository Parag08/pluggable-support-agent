import json
from datetime import timedelta

from app.agent.contracts import Entities
from app.auth.grant import mark_identity_validated
from app.agent.llm import LLMError, LLMResult
from app.db.base import utcnow

EMPTY_ENTITIES = {name: None for name in Entities.model_fields}


def classification(
    intent: str,
    *,
    confidence: float = 0.95,
    entities: dict | None = None,
    secondary: list[str] | None = None,
    sentiment: str = "neutral",
    injection: bool = False,
    techniques: list[str] | None = None,
    images: list[dict] | None = None,
) -> str:
    return json.dumps({
        "contract_version": "1",
        "intent": intent,
        "confidence": confidence,
        "secondary_intents": secondary or [],
        "sentiment": sentiment,
        "injection": {"detected": injection, "techniques": techniques or []},
        "images": images or [],
        "entities": {**EMPTY_ENTITIES, **(entities or {})},
    })


def reply(text: str) -> str:
    return json.dumps({"reply": text})


class FakeLLM:
    """Scripted stand-in for the LLM provider. Each push() answers the next complete() call."""

    provider = "fake"

    def __init__(self):
        self.queue: list[dict] = []
        self.calls: list[dict] = []

    def push(self, content: str | None = None, *, finish_reason: str = "stop", error: str | None = None) -> "FakeLLM":
        self.queue.append({"content": content, "finish_reason": finish_reason, "error": error})
        return self

    def complete(self, **kwargs) -> LLMResult:
        self.calls.append(kwargs)
        if not self.queue:
            raise AssertionError(f"unexpected LLM call ({kwargs['schema_name']})")
        item = self.queue.pop(0)
        if item["error"]:
            raise LLMError(item["error"])
        return LLMResult(item["content"], item["finish_reason"], kwargs["model"], 120, 30, 5, {})

    def calls_for(self, schema_name: str) -> list[dict]:
        return [c for c in self.calls if c["schema_name"] == schema_name]


def sign_in(db, session, customer_id: str) -> None:
    """Stand-in for the auth module's OIDC callback."""
    now = utcnow()
    mark_identity_validated(db, session.id, customer_id, now, now + timedelta(hours=1))
    db.commit()
