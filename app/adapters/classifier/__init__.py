"""Classifier plugins (the ClassifierPort adapters).

`CLASSIFIER_PROVIDER` in `.env` picks one:

- `llm_oneshot`: one LLM call per message, using whichever LLM provider is configured.
- `<python.module>:<ClassName>`: your own classifier. It is constructed as
  `Class(settings=..., config=..., llm=...)` and must implement `app.ports.classifier.Classifier`.
"""

import importlib

from app.agent.config_loader import AgentConfig
from app.agent.llm import LLMClient
from app.config import Settings
from app.ports.classifier import Classifier

BUILT_IN = {"llm_oneshot": "app.adapters.classifier.llm_oneshot:OneShotLLMClassifier"}


def build_classifier(settings: Settings, config: AgentConfig, llm: LLMClient) -> Classifier:
    target = BUILT_IN.get(settings.classifier_provider, settings.classifier_provider)
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unknown CLASSIFIER_PROVIDER {settings.classifier_provider!r}: use one of {sorted(BUILT_IN)} "
            "or '<python.module>:<ClassName>'"
        )
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(settings=settings, config=config, llm=llm)
