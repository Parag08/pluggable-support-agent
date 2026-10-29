"""Stage 5: write the customer-facing reply for informational answers.

The responder sees only structured DATA (intent + handler output). It has no
tools and cannot trigger actions; its reply then goes through the output check.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.agent.config_loader import AgentConfig
from app.agent.contracts import parse_responder_output
from app.agent.llm import LLMClient, LLMError, LLMResult
from app.config import Settings

RESPONDER_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


@dataclass
class ResponderRun:
    request_messages: list[dict[str, Any]]
    result: LLMResult | None
    reply: str | None
    error: str | None
    model: str
    system_prompt_hash: str
    params: dict[str, Any]


class Responder:
    def __init__(self, llm: LLMClient, settings: Settings, config: AgentConfig):
        self.llm = llm
        self.s = settings
        self.system_prompt = config.responder_prompt
        self.system_prompt_hash = hashlib.sha256(self.system_prompt.encode()).hexdigest()

    def write(self, intent: str, data: dict[str, Any], customer_first_name: str | None) -> ResponderRun:
        payload = {"intent": intent, "customer_first_name": customer_first_name, "data": data}
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "DATA:\n" + json.dumps(payload, default=str)},
        ]
        params = {"max_tokens": self.s.responder_max_tokens, "temperature": self.s.responder_temperature,
                  "json_mode": self.s.llm_json_mode}
        run = ResponderRun(messages, None, None, None, self.s.responder_model, self.system_prompt_hash, params)
        try:
            run.result = self.llm.complete(
                model=self.s.responder_model,
                messages=messages,
                max_tokens=self.s.responder_max_tokens,
                temperature=self.s.responder_temperature,
                json_schema=RESPONDER_SCHEMA,
                schema_name="support_reply",
            )
        except LLMError as exc:
            run.error = f"LLM_ERROR: {exc}"
            return run
        if run.result.finish_reason not in ("stop", None):
            run.error = f"BAD_FINISH_REASON: {run.result.finish_reason}"
            return run
        parsed = parse_responder_output(run.result.content)
        if parsed is None or not parsed.reply.strip():
            run.error = "INVALID_RESPONDER_OUTPUT"
            return run
        run.reply = parsed.reply.strip()
        return run
