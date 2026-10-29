"""IdentityPort adapters.

`IDENTITY_PROVIDER` in `.env` picks one:

- `signed_jwt`: the company's site (already logged in) signs a short-lived token for the chat.
- `dev`: development only. The "token" is simply a customer id. Never use in production.
- `<python.module>:<ClassName>`: your own adapter (OIDC, Auth0, ...), constructed with `Settings`.
"""

import importlib

from app.config import Settings
from app.ports.identity import IdentityPort

BUILT_IN = {
    "signed_jwt": "app.adapters.identity.signed_jwt:SignedJwtIdentity",
    "dev": "app.adapters.identity.dev:DevIdentity",
}


def build_identity(settings: Settings) -> IdentityPort:
    target = BUILT_IN.get(settings.identity_provider, settings.identity_provider)
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unknown IDENTITY_PROVIDER {settings.identity_provider!r}: use one of {sorted(BUILT_IN)} "
            "or '<python.module>:<ClassName>'"
        )
    return getattr(importlib.import_module(module_name), class_name)(settings)
