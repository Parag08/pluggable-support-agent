"""Any OpenAI-compatible chat-completions API (Nous Portal, OpenAI, OpenRouter, vLLM, Ollama, ...)."""

import time
from typing import Any

import openai
from openai import OpenAI

from app.agent.llm import LLMError, LLMResult
from app.config import Settings


def response_format(mode: str, json_schema: dict[str, Any] | None, schema_name: str) -> dict[str, Any] | None:
    if json_schema is None or mode == "prompt":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": json_schema}}


class OpenAICompatibleLLMClient:
    provider = "openai_compatible"

    def __init__(self, settings: Settings):
        key = settings.llm_api_key.get_secret_value()
        if not key:
            raise RuntimeError("LLM_API_KEY is empty: add your provider's key to .env")
        self._json_mode = settings.llm_json_mode
        self._client = OpenAI(
            api_key=key,
            base_url=settings.llm_base_url or None,  # empty = api.openai.com
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

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
        if not model:
            raise RuntimeError("Model ID is empty: set CLASSIFIER_MODEL / RESPONDER_MODEL in .env")
        kwargs: dict[str, Any] = {}
        fmt = response_format(self._json_mode, json_schema, schema_name)
        if fmt is not None:
            kwargs["response_format"] = fmt

        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
        except openai.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - t0) * 1000)

        choice = resp.choices[0] if resp.choices else None
        usage = resp.usage
        return LLMResult(
            content=choice.message.content if choice else None,
            finish_reason=choice.finish_reason if choice else None,
            model=resp.model or model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=latency_ms,
            raw=resp.model_dump(),
        )
