"""Stage 2: turn classifier entities into validated, typed fields for an intent.

Values are merged into the active intent's slots (kept in session state) so the
customer is never asked twice. Slot values are JSON-serialisable.
Fields that depend on the customer's own order (which item, exchange target,
recent-order picker) are only resolved once identity is validated and the order
belongs to that customer — otherwise the policy stage handles it.
"""

import difflib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent.config_loader import AgentConfig, IntentSpec
from app.agent.contracts import Entities
from app.config import Settings
from app.core.types import OrderInfo, OrderItemInfo, ProductInfo
from app.ports.commerce import CommercePort

TEXT_PASSTHROUGH = ("question", "description", "location", "country", "reference_brand", "item_hint")
ENUM_PASSTHROUGH = ("size_system", "width", "new_width", "return_reason", "change_type")
SIZE_FIELDS = ("reference_size", "size", "new_size")
HALF = Decimal("0.5")


@dataclass
class Validation:
    slots: dict[str, Any]
    missing: list[str] = field(default_factory=list)
    invalid: dict[str, str] = field(default_factory=dict)  # field -> template key
    quick_replies: dict[str, list[dict]] = field(default_factory=dict)  # field -> options
    order: OrderInfo | None = None
    item: OrderItemInfo | None = None

    def to_log(self) -> dict:
        return {"slots": self.slots, "missing": self.missing, "invalid": self.invalid}


def parse_size(raw: str, size_system: str | None) -> Decimal | None:
    m = re.search(r"\d+(?:[.,]\d+)?", raw)
    if not m:
        return None
    try:
        value = Decimal(m.group(0).replace(",", "."))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    if size_system != "CM" and value % HALF != 0:
        return None
    return value


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def resolve_product(products: list[ProductInfo], text: str, matching: dict) -> tuple[ProductInfo | None, list[ProductInfo]]:
    """Return (match, suggestions). A match is only returned when it's unambiguous.

    Gender words ("for women", "men's") narrow the match; filler words are ignored.
    """
    words = _words(text)
    gender = next((g for g, vocab in matching["gender_words"].items() if any(w in vocab for w in words)), None)
    ignore = set(matching["stop_words"]) | {w for vocab in matching["gender_words"].values() for w in vocab}
    needle = " ".join(w for w in words if w not in ignore)

    def base(p: ProductInfo) -> str:
        return " ".join(_words(p.name.split(" (")[0]))

    def narrow(candidates: list[ProductInfo]) -> list[ProductInfo]:
        if gender and len(candidates) > 1:
            gendered = [p for p in candidates if p.gender.value in (gender, "unisex")]
            return gendered or candidates
        return candidates

    exact = narrow([p for p in products if needle and needle in (base(p), p.handle.replace("-", " "))])
    if len(exact) == 1:
        return exact[0], []
    contains = narrow([p for p in products if needle and (needle in base(p) or base(p) in needle)])
    if len(contains) == 1:
        return contains[0], []
    by_base: dict[str, list[ProductInfo]] = {}
    for p in products:
        by_base.setdefault(base(p), []).append(p)
    close = difflib.get_close_matches(needle, list(by_base), n=3, cutoff=matching["fuzzy_cutoff"])
    fuzzy = narrow([p for c in close for p in by_base[c]])
    if len(fuzzy) == 1 and not contains:
        return fuzzy[0], []
    return None, (contains or fuzzy or list(products))[: matching["max_suggestions"]]


def _match_color(product: ProductInfo, raw: str) -> str | None:
    for c in product.colors:
        if c.lower() == raw.strip().lower():
            return c
    return None


def _item_label(item: OrderItemInfo) -> str:
    return f"{item.product_name} · size {item.size_us} {item.width.value} · {item.color}"


def validate(
    commerce: CommercePort,
    spec: IntentSpec,
    slots: dict[str, Any],
    entities: Entities | None,
    *,
    identity_valid: bool,
    customer_id: str | None,
    settings: Settings,
    config: AgentConfig,
) -> Validation:
    v = Validation(slots=dict(slots))
    e = entities.model_dump(mode="json") if entities else {}

    # --- normalise what the customer just told us ------------------------
    for name in TEXT_PASSTHROUGH:
        if e.get(name):
            v.slots[name] = e[name].strip()
    for name in ENUM_PASSTHROUGH:
        if e.get(name):
            v.slots[name] = e[name]

    if e.get("order_id"):
        oid = re.sub(r"[\s#]", "", e["order_id"]).upper()
        if re.fullmatch(settings.order_id_pattern, oid):
            if oid != v.slots.get("order_id"):
                # New order → drop fields that belonged to the previous one.
                for dep in ("order_item_id", "exchange_target"):
                    v.slots.pop(dep, None)
            v.slots["order_id"] = oid
        else:
            v.invalid["order_id"] = "invalid_order_id"

    for name in SIZE_FIELDS:
        if e.get(name):
            size = parse_size(e[name], v.slots.get("size_system"))
            if size is None:
                v.invalid[name] = "invalid_size"
            else:
                v.slots[name] = str(size)

    if e.get("product"):
        product, suggestions = resolve_product(commerce.list_products(), e["product"], config.product_matching)
        if product:
            v.slots["product"] = product.id
        elif "product" in spec.required:
            v.invalid["product"] = "invalid_product"
            v.quick_replies["product"] = [{"label": p.name} for p in suggestions]
        else:
            # Not needed for this intent (e.g. "return the flats") — use it to pick the order item.
            v.slots.setdefault("item_hint", e["product"].strip())

    for name in ("color", "new_color"):
        if e.get(name):
            product = commerce.get_product(v.slots["product"]) if v.slots.get("product") else None
            v.slots[name] = (_match_color(product, e[name]) if product else None) or e[name].strip()

    if e.get("discount_code"):
        code = re.sub(r"\s", "", e["discount_code"]).upper()
        if re.fullmatch(settings.discount_code_pattern, code):
            v.slots["discount_code"] = code
        else:
            v.invalid["discount_code"] = "invalid_discount_code"

    # --- fields that come from the customer's own orders ------------------
    if spec.protected and identity_valid and customer_id:
        _resolve_order_fields(commerce, spec, v, customer_id, settings)

    # --- what is still missing -------------------------------------------
    # Protected intents ask for sign-in before anything else (policy stage).
    order_not_owned = spec.protected and identity_valid and "order_id" in v.slots and v.order is None
    if order_not_owned:
        v.missing = []  # don't ask for details about an order that isn't theirs — policy denies it
    elif not spec.protected or identity_valid:
        v.missing = [f for f in spec.required if f not in v.slots]
        for f in v.missing:
            if f == "return_reason":
                v.quick_replies.setdefault(f, [
                    {"label": label} for label in config.templates["return_reason_labels"].values()
                ])
            elif f == "change_type":
                v.quick_replies.setdefault(f, [
                    {"label": label} for label in config.templates["change_type_labels"].values()
                ])
    return v


def _resolve_order_fields(commerce: CommercePort, spec: IntentSpec, v: Validation, customer_id: str,
                          settings: Settings) -> None:
    if "order_id" not in v.slots:
        recent = commerce.list_customer_orders(customer_id, settings.order_picker_limit)
        v.quick_replies["order_id"] = [
            {"label": f"{o.id} · {o.placed_at:%b %d} · {o.status.value}"} for o in recent
        ]
        return

    order = commerce.get_customer_order(customer_id, v.slots["order_id"])
    if order is None:
        return  # policy stage denies without revealing whether the order exists
    v.order = order

    if "order_item_id" in spec.required:
        items = order.items
        chosen = None
        if v.slots.get("order_item_id"):
            chosen = next((i for i in items if i.id == v.slots["order_item_id"]), None)
        elif len(items) == 1:
            chosen = items[0]
        else:
            hint = (v.slots.get("item_hint") or "").lower()
            product_id = v.slots.get("product")
            matches = [
                i for i in items
                if (product_id and i.product_id == product_id)
                or (hint and (hint in i.product_name.lower() or i.product_name.lower().split(" (")[0] in hint))
            ]
            if len(matches) == 1:
                chosen = matches[0]
        if chosen:
            v.slots["order_item_id"] = chosen.id
            v.item = chosen
        else:
            v.slots.pop("order_item_id", None)
            v.quick_replies["order_item_id"] = [{"label": _item_label(i)} for i in items]

    if "exchange_target" in spec.required and v.item is not None:
        if any(v.slots.get(k) for k in ("new_size", "new_width", "new_color")):
            v.slots["exchange_target"] = {
                "size_us": v.slots.get("new_size") or str(v.item.size_us),
                "width": v.slots.get("new_width") or v.item.width.value,
                "color": v.slots.get("new_color") or v.item.color,
            }
