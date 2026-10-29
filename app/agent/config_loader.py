"""Load the agent's data-driven configuration from AGENT_CONFIG_DIR."""

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.db.models import EscalationPriority, EscalationReason, IntentType


@dataclass(frozen=True)
class IntentSpec:
    name: str
    label: str
    type: IntentType
    kind: str  # answer | action | escalate | decline
    description: str
    required: tuple[str, ...]
    requires: tuple[str, ...] = ()  # CommercePort capabilities this intent needs
    handler: str | None = None
    escalation_reason: EscalationReason | None = None
    escalation_priority: EscalationPriority | None = None

    @property
    def protected(self) -> bool:
        return self.type == IntentType.PROTECTED


@dataclass(frozen=True)
class AgentConfig:
    intents: dict[str, IntentSpec]
    write_intents: frozenset[str]
    templates: dict[str, Any]
    classifier_prompt: str
    responder_prompt: str
    redaction: dict[str, Any]
    output_check: dict[str, Any]
    injection_guard: dict[str, Any]
    policies: dict[str, Any]
    product_matching: dict[str, Any]
    pricing: dict[str, dict[str, str]] = field(default_factory=dict)
    brand_name: str = ""

    def template(self, key: str, **params: Any) -> str:
        return self.templates[key].format(**{"brand_name": self.brand_name, **params})


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_agent_config(settings: Settings) -> AgentConfig:
    d = settings.agent_config_dir
    brand = settings.brand_name

    def branded(text: str) -> str:
        return text.replace("{{BRAND_NAME}}", brand)

    raw = _read_json(d / "intents.json")
    intents = {}
    for i in raw["intents"]:
        intents[i["name"]] = IntentSpec(
            name=i["name"],
            label=i["label"],
            type=IntentType(i["type"]),
            kind=i["kind"],
            description=branded(i["description"]),
            required=tuple(i["required"]),
            requires=tuple(i.get("requires", [])),
            handler=i.get("handler"),
            escalation_reason=EscalationReason(i["escalation_reason"]) if i.get("escalation_reason") else None,
            escalation_priority=EscalationPriority(i["escalation_priority"]) if i.get("escalation_priority") else None,
        )
    pricing = {k: v for k, v in _read_json(d / "pricing.json").items() if not k.startswith("_")}
    product_matching = _read_json(d / "product_matching.json")
    # The brand name is filler when matching products ("the Acme Footwear runner").
    product_matching["stop_words"] = [*product_matching["stop_words"], *brand.lower().split()]
    return AgentConfig(
        intents=intents,
        write_intents=frozenset(raw["write_intents_need_higher_confidence"]),
        templates=_read_json(d / "templates.json"),
        classifier_prompt=branded((d / "prompts" / "classifier.md").read_text(encoding="utf-8")),
        responder_prompt=branded((d / "prompts" / "responder.md").read_text(encoding="utf-8")),
        redaction=_read_json(d / "redaction.json"),
        output_check=_read_json(d / "output_check.json"),
        injection_guard=_read_json(d / "injection_guard.json"),
        policies={k: v for k, v in _read_json(d / "policies.json").items() if not k.startswith("_")},
        product_matching=product_matching,
        pricing=pricing,
        brand_name=brand,
    )


@lru_cache
def get_agent_config() -> AgentConfig:
    return load_agent_config(get_settings())
