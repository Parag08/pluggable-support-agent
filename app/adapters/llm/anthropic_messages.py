"""Claude through the Anthropic Messages API.

Translates the pipeline's OpenAI-shaped messages to the Messages API:
the system message becomes the top-level `system` (cached), data-URL images
become base64 image blocks, and the JSON schema becomes structured output
(`output_config.format`). `temperature` is not sent: current Claude models
reject sampling parameters. Depth/cost is set with `LLM_EFFORT` instead.

Requires `pip install anthropic`. An empty `LLM_API_KEY` lets the SDK find
credentials itself (ANTHROPIC_API_KEY, or an `ant auth login` profile).
"""

import time
from typing import Any

from app.agent.llm import LLMError, LLMResult
from app.config import Settings

# Messages API stop reasons → the normalised values the pipeline checks.
FINISH_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter"}


def _image_block(data_url: str) -> dict[str, Any]:
    header, _, data = data_url.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def to_messages_api(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    converted = []
    for m in messages:
        if m["role"] == "system":
            continue
        content = m["content"]
        if isinstance(content, list):
            content = [
                _image_block(part["image_url"]["url"]) if part.get("type") == "image_url" else part
                for part in content
            ]
        converted.append({"role": m["role"], "content": content})
    return system, converted


class AnthropicLLMClient:
    provider = "anthropic"

    def __init__(self, settings: Settings, client: Any = None):
        self._json_mode = settings.llm_json_mode
        self._effort = settings.llm_effort
        if client is None:
            import anthropic  # optional dependency: only needed when this provider is selected

            key = settings.llm_api_key.get_secret_value()
            client = anthropic.Anthropic(
                api_key=key or None,
                base_url=settings.llm_base_url or None,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        self._client = client

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
        schema_name: str,
    ) -> LLMResult:
        import anthropic

        if not model:
            raise RuntimeError("Model ID is empty: set CLASSIFIER_MODEL / RESPONDER_MODEL in .env")
        system, converted = to_messages_api(messages)
        output_config: dict[str, Any] = {}
        if json_schema is not None and self._json_mode != "prompt":
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if self._effort:
            output_config["effort"] = self._effort
        kwargs: dict[str, Any] = {"output_config": output_config} if output_config else {}

        t0 = time.perf_counter()
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # The system prompt is the same on every call, so cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=converted,
                **kwargs,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - t0) * 1000)

        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = resp.usage
        input_tokens = None
        if usage is not None:
            input_tokens = (usage.input_tokens + (usage.cache_read_input_tokens or 0)
                            + (usage.cache_creation_input_tokens or 0))
        return LLMResult(
            content=text or None,
            finish_reason=FINISH_REASONS.get(resp.stop_reason, resp.stop_reason),
            model=resp.model or model,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens if usage else None,
            latency_ms=latency_ms,
            raw=resp.model_dump(),
        )
