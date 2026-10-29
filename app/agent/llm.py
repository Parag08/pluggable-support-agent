"""The inference contract the agent depends on.

The pipeline only knows the `LLMClient` protocol below. Which provider serves it
(any OpenAI-compatible endpoint, Anthropic, or a custom plugin) is chosen in
`.env` with `LLM_PROVIDER` and built by `app.adapters.llm.build_llm_client`.
Tests use a scripted fake, so they make no network calls.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


class LLMError(Exception):
    """The provider call failed (network, HTTP error, timeout) after retries."""


@dataclass
class LLMResult:
    content: str | None
    # Normalised across providers: "stop" (finished), "length" (truncated),
    # "content_filter" (refused); anything else is treated as a failure.
    finish_reason: str | None
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """What a provider plugin must implement.

    `messages` use the OpenAI chat shape: a leading {"role": "system"} message, then
    {"role": "user"} messages whose content is a string or a list of
    {"type": "text"} / {"type": "image_url", "image_url": {"url": "data:..."}} parts.
    Adapters translate this to their provider's format.
    """

    provider: str  # recorded on every llm_calls row

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        schema_name: str,
    ) -> LLMResult: ...


def compute_cost(pricing: dict[str, dict[str, str]], model: str, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
    price = pricing.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return Decimal(price["prompt"]) * input_tokens + Decimal(price["completion"]) * output_tokens
