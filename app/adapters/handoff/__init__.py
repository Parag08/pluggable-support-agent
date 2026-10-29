"""HandoffPort adapters.

`HANDOFF_PROVIDER` in `.env` picks one:

- `local`: no external ticket. The escalation record in the module's own store is the handoff.
- `email`: emails the summary and transcript to a support address over SMTP.
- `<python.module>:<ClassName>`: your own adapter (Zendesk, Freshdesk, Gorgias, ...),
  constructed with the `Settings` object.
"""

import importlib

from app.config import Settings
from app.ports.handoff import HandoffPort

BUILT_IN = {
    "local": "app.adapters.handoff.local:LocalHandoff",
    "email": "app.adapters.handoff.email:EmailHandoff",
}


def build_handoff(settings: Settings) -> HandoffPort:
    target = BUILT_IN.get(settings.handoff_provider, settings.handoff_provider)
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unknown HANDOFF_PROVIDER {settings.handoff_provider!r}: use one of {sorted(BUILT_IN)} "
            "or '<python.module>:<ClassName>'"
        )
    return getattr(importlib.import_module(module_name), class_name)(settings)
