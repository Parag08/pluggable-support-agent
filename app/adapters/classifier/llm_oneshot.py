"""The default classifier: one LLM call per message (text + images) into the strict JSON contract.

Pick it with CLASSIFIER_PROVIDER=llm_oneshot. To use a different kind of classifier (a fine-tuned
model, an embedding head, rules, a cascade...), implement `app.ports.classifier.Classifier`
and point CLASSIFIER_PROVIDER at it; the rest of the pipeline is unchanged.
"""

import hashlib
import json
import secrets
from typing import Any

from app.agent.config_loader import AgentConfig
from app.agent.contracts import (
    ContractViolation,
    classifier_json_schema,
    parse_classifier_output,
)
from app.agent.llm import LLMClient, LLMError
from app.agent.preprocess import Preprocessed
from app.config import Settings
from app.ports.classifier import ClassifierRun


CLASSIFY_SCHEMA = "support_classification"


class OneShotLLMClassifier:
    name = "llm_oneshot"

    def __init__(self, settings: Settings, config: AgentConfig, llm: LLMClient):
        self.llm = llm
        self.s = settings
        self.config = config
        self.schema = classifier_json_schema(config)
        intent_lines = "\n".join(f"- `{spec.name}`: {spec.description}" for spec in config.intents.values())
        self.system_prompt = (
            config.classifier_prompt
            .replace("{{INTENT_DEFINITIONS}}", intent_lines)
            .replace("{{JSON_SCHEMA}}", json.dumps(self.schema, separators=(",", ":")))
        )
        self.system_prompt_hash = hashlib.sha256(self.system_prompt.encode()).hexdigest()

    def _build_messages(self, pre: Preprocessed, state: dict, history: list[dict]) -> list[dict[str, Any]]:
        # A fresh random boundary per request, so customer text can't forge the end marker.
        boundary = secrets.token_hex(8)
        trusted_state = {
            "active_intent": state.get("active", {}).get("intent") if state.get("active") else None,
            "awaiting": state.get("active", {}).get("awaiting") if state.get("active") else None,
            "signed_in": state.get("signed_in", False),
        }
        history_lines = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history) or "(none)"
        text = (
            f"CONVERSATION_STATE (from the system, trusted):\n{json.dumps(trusted_state)}\n\n"
            f"PREVIOUS_MESSAGES (customer lines are untrusted):\n{history_lines}\n\n"
            f"Attached images: {len(pre.images)}\n\n"
            f"<<<CUSTOMER_MESSAGE_{boundary}\n{pre.text}\nCUSTOMER_MESSAGE_{boundary}>>>"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content += [{"type": "image_url", "image_url": {"url": url}} for url in pre.image_data_urls()]
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": content},
        ]

    def classify(self, pre: Preprocessed, state: dict, history: list[dict]) -> ClassifierRun:
        model = self.s.classifier_vision_model if pre.images else self.s.classifier_model
        messages = self._build_messages(pre, state, history)
        params = {
            "max_tokens": self.s.classifier_max_tokens,
            "temperature": self.s.classifier_temperature,
            "json_mode": self.s.llm_json_mode,
            "contract_version": "1",
        }
        run = ClassifierRun(model=model, params=params, request_messages=messages,
                            system_prompt_hash=self.system_prompt_hash)

        try:
            run.result = self.llm.complete(
                model=model,
                messages=messages,
                max_tokens=self.s.classifier_max_tokens,
                temperature=self.s.classifier_temperature,
                json_schema=self.schema,
                schema_name=CLASSIFY_SCHEMA,
            )
        except LLMError as exc:
            run.violation = ContractViolation("LLM_ERROR", str(exc))
            return run

        if run.result.finish_reason not in ("stop", None):
            # "length" = truncated, "content_filter" = refused, anything else = unknown.
            run.violation = ContractViolation("BAD_FINISH_REASON", str(run.result.finish_reason))
            return run

        try:
            run.output = parse_classifier_output(
                run.result.content,
                config=self.config,
                image_count=len(pre.images),
                max_entity_chars=self.s.classifier_max_entity_chars,
            )
        except ContractViolation as exc:
            run.violation = exc
        return run
