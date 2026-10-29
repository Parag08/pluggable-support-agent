"""CommercePort adapters.

`COMMERCE_PROVIDER` in `.env` picks one:

- `sql_reference`: the demo SQL schema (`app/db/models/commerce.py`). Doubles as the
  reference implementation that other adapters are tested against.
- `http_json`: any company system that exposes the small JSON-over-HTTP contract in
  `app/adapters/commerce/http_json.py`, written in any language.
- `<python.module>:<ClassName>`: your own Python adapter, constructed with the `Settings` object.
"""

import importlib

from sqlalchemy.orm import Session

from app.config import Settings
from app.ports.commerce import CommercePort

BUILT_IN = {
    "sql_reference": "app.adapters.commerce.sql_reference:SqlReferenceCommerce",
    "http_json": "app.adapters.commerce.http_json:HttpJsonCommerce",
}


def build_commerce(settings: Settings, db: Session | None = None) -> CommercePort:
    target = BUILT_IN.get(settings.commerce_provider, settings.commerce_provider)
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unknown COMMERCE_PROVIDER {settings.commerce_provider!r}: use one of {sorted(BUILT_IN)} "
            "or '<python.module>:<ClassName>'"
        )
    cls = getattr(importlib.import_module(module_name), class_name)
    if settings.commerce_provider == "sql_reference":
        if db is None:
            raise RuntimeError("COMMERCE_PROVIDER=sql_reference needs a database session")
        return cls(db, settings)
    return cls(settings)
