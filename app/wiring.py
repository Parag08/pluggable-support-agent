"""Composition root: the only place that chooses adapters.

The agent core (`app/agent/`) is given ready-made ports and never imports an adapter.
Each adapter is picked by a `*_PROVIDER` setting in `.env`; pass your own object for any
port to override it (tests do this with fakes).
"""

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.adapters.classifier import build_classifier
from app.adapters.commerce import build_commerce
from app.adapters.conversation.sql_store import SqlConversationStore
from app.adapters.handoff import build_handoff
from app.adapters.identity import build_identity
from app.adapters.llm import build_llm_client
from app.agent.config_loader import AgentConfig, get_agent_config
from app.agent.llm import LLMClient
from app.agent.orchestrator import SupportAgent, utcnow
from app.config import Settings, get_settings
from app.ports.classifier import Classifier
from app.ports.commerce import CommercePort
from app.ports.handoff import HandoffPort
from app.ports.identity import IdentityPort

__all__ = ["build_agent", "build_identity"]


def build_agent(
    db: Session,
    settings: Settings | None = None,
    *,
    config: AgentConfig | None = None,
    llm: LLMClient | None = None,
    classifier: Classifier | None = None,
    commerce: CommercePort | None = None,
    handoff: HandoffPort | None = None,
    identity: IdentityPort | None = None,
    now_fn: Callable[[], datetime] = utcnow,
) -> SupportAgent:
    s = settings or get_settings()
    cfg = config or get_agent_config()
    llm = llm or build_llm_client(s)
    return SupportAgent(
        commerce or build_commerce(s, db),
        SqlConversationStore(db),
        llm=llm,
        classifier=classifier or build_classifier(s, cfg, llm),
        handoff=handoff or build_handoff(s),
        identity=identity or build_identity(s),
        settings=s,
        config=cfg,
        now_fn=now_fn,
    )
