"""LLM provider plugins (the LLMPort adapters).

`LLM_PROVIDER` in `.env` picks one:

- `openai_compatible`: any OpenAI-compatible chat-completions API (Nous Portal,
  OpenAI, OpenRouter, Together, Groq, vLLM, Ollama, ...). Set `LLM_BASE_URL`.
- `anthropic`: Claude through the Anthropic Messages API (`pip install anthropic`).
- `<python.module>:<ClassName>`: your own plugin. The class is constructed with the
  `Settings` object and must implement `app.agent.llm.LLMClient`.
"""

import importlib

from app.agent.llm import LLMClient
from app.config import Settings

BUILT_IN = {
    "openai_compatible": "app.adapters.llm.openai_compatible:OpenAICompatibleLLMClient",
    "anthropic": "app.adapters.llm.anthropic_messages:AnthropicLLMClient",
}


def build_llm_client(settings: Settings) -> LLMClient:
    target = BUILT_IN.get(settings.llm_provider, settings.llm_provider)
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER {settings.llm_provider!r}: use one of {sorted(BUILT_IN)} "
            "or '<python.module>:<ClassName>' for a plugin"
        )
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(settings)
