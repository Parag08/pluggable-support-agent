"""Stage 6: deterministic checks on model-written replies.

A reply may only mention order IDs, return IDs, emails and money amounts that
appear in the data it was given. This catches hallucinated facts and any leak
of data that wasn't part of this customer's answer.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent.preprocess import _luhn_ok


def _numbers_in(data: Any) -> set[Decimal]:
    found: set[Decimal] = set()
    for m in re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?", json.dumps(data, default=str)):
        try:
            found.add(Decimal(m.group(0).replace(",", "")).normalize())
        except InvalidOperation:
            pass
    return found


def check_reply(
    reply: str,
    data: dict[str, Any],
    *,
    cfg: dict[str, Any],
    max_chars: int,
    customer_email: str | None,
) -> list[str]:
    """Return a list of failure codes (empty = passed)."""
    failures: list[str] = []
    data_text = json.dumps(data, default=str)
    lower = reply.lower()

    if len(reply) > max_chars:
        failures.append("TOO_LONG")
    for phrase in cfg["banned_phrases"]:
        if phrase in lower:
            failures.append(f"BANNED_PHRASE:{phrase}")
    for m in re.finditer(cfg["card_number_pattern"], reply):
        if _luhn_ok(re.sub(r"\D", "", m.group(0))):
            failures.append("CARD_NUMBER")
    for m in re.finditer(cfg["email_pattern"], reply):
        if m.group(0) not in data_text and m.group(0).lower() != (customer_email or "").lower():
            failures.append("UNKNOWN_EMAIL")
    for key in ("order_id_pattern", "return_id_pattern"):
        for m in re.finditer(cfg[key], reply):
            if m.group(0) not in data_text:
                failures.append(f"UNKNOWN_ID:{m.group(0)}")
    allowed = _numbers_in(data)
    for m in re.finditer(cfg["money_pattern"], reply):
        amount = Decimal(m.group(1).replace(",", "")).normalize()
        if amount not in allowed:
            failures.append(f"UNSUPPORTED_AMOUNT:{m.group(0)}")
    return failures
