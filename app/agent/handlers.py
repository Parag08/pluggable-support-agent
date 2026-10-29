"""Stage 4: action handlers.

Handlers only call `CommercePort` functions — they never touch a database or a vendor API.
Read handlers return plain dicts holding only what the customer may see; that
dict is the *only* factual input the responder gets. Write handlers run only
after policy returned CONFIRM and the customer confirmed.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.agent.policy import money, refund_estimate
from app.config import Settings
from app.core.types import OrderInfo, VariantInfo
from app.ports.commerce import CommercePort
from app.db.models import Gender

HALF = Decimal("0.5")


def _date(value: datetime | date | None) -> str | None:
    return None if value is None else f"{value:%B %d, %Y}"


def _availability(variants: list[VariantInfo]) -> list[dict]:
    return [
        {"color": v.color, "width": v.width.value, "in_stock": v.stock_qty > 0,
         "restock_date": _date(v.restock_date) if v.restock_date else None}
        for v in sorted(variants, key=lambda v: (v.color, v.width.value))
    ]


class Handlers:
    def __init__(self, commerce: CommercePort, settings: Settings, policies: dict[str, Any]):
        self.commerce = commerce
        self.s = settings
        self.policies = policies

    # --- public -----------------------------------------------------------

    def size_advice(self, slots: dict) -> dict:
        product = self.commerce.get_product(slots["product"])
        chart_gender = Gender.MEN if product.gender == Gender.UNISEX else product.gender
        system = slots.get("size_system") or "US"
        ref = Decimal(slots["reference_size"])
        us = ref if system == "US" else self.commerce.convert_to_us_size(chart_gender, system, ref)

        data: dict[str, Any] = {
            "product": product.name,
            "fit_note": product.fit_note_detail,
            "customer_size": {"value": str(ref), "system": system, "brand": slots.get("reference_brand")},
            "customer_size_us": None if us is None else str(us),
            "available_widths": product.widths,
        }
        if us is None:
            data["recommendation"] = None
            return data

        recommended = ((us + product.fit_size_offset) / HALF).to_integral_value() * HALF
        variants = [v for v in product.variants if v.size_us == recommended]
        data["recommended_size_us"] = str(recommended)
        data["size_offered"] = bool(variants)
        data["availability"] = _availability(variants)
        return data

    def product_info(self, slots: dict) -> dict:
        data: dict[str, Any] = {"customer_question": slots.get("question")}
        if slots.get("product"):
            p = self.commerce.get_product(slots["product"])
            data["product"] = {
                "name": p.name, "description": p.description, "materials": list(p.materials),
                "features": list(p.features), "care": p.care_instructions, "fit": p.fit_note_detail,
                "price": money(self.s, p.price), "final_sale": p.is_final_sale,
                "colors": p.colors, "widths": p.widths,
            }
        else:
            data["catalog"] = [
                {"name": p.name, "category": p.category, "features": list(p.features), "price": money(self.s, p.price)}
                for p in self.commerce.list_products()
            ]
        return data

    def stock_check(self, slots: dict) -> dict:
        product = self.commerce.get_product(slots["product"])
        size = Decimal(slots["size"])
        q = [v for v in product.variants if v.size_us == size]
        if slots.get("width"):
            q = [v for v in q if v.width.value == slots["width"]]
        if slots.get("color"):
            q = [v for v in q if v.color.lower() == slots["color"].lower()]
        return {"product": product.name, "size_us": str(size), "size_offered": bool(q), "variants": _availability(q)}

    def shipping_policy(self, slots: dict) -> dict:
        return {
            "customer_question": slots.get("question"),
            "free_shipping_over": money(self.s, self.s.free_shipping_threshold),
            "standard_shipping_fee": money(self.s, self.s.standard_shipping_fee),
            "return_window_days": self.s.return_window_days,
            **self.policies,
        }

    def store_info(self, slots: dict) -> dict:
        stores = self.commerce.list_stores()
        where = (slots.get("location") or "").lower()
        if where:
            matched = [s for s in stores if where in f"{s.city} {s.region} {s.postal_code} {s.name}".lower()]
            stores = matched or stores
        return {
            "searched_location": slots.get("location"),
            "stores": [
                {"name": s.name, "address": f"{s.address_line}, {s.city}, {s.region} {s.postal_code}",
                 "phone": s.phone, "hours": s.opening_hours, "try_on": s.offers_try_on}
                for s in stores
            ],
        }

    def discount_code(self, slots: dict, now: datetime) -> dict:
        code = self.commerce.get_discount_code(slots["discount_code"])
        if code is None:
            return {"code": slots["discount_code"], "valid": False, "reason": "not_found"}
        reason = None
        if not code.is_active or (code.ends_at and code.ends_at < now):
            reason = "expired"
        elif code.starts_at and code.starts_at > now:
            reason = "not_started"
        return {
            "code": code.code, "valid": reason is None, "reason": reason, "description": code.description,
            "minimum_order": money(self.s, code.min_subtotal) if code.min_subtotal else None,
        }

    # --- protected (customer_id comes only from the validated identity) ----

    def _owned_order(self, customer_id: str, order_id: str) -> OrderInfo:
        order = self.commerce.get_customer_order(customer_id, order_id)
        if order is None:
            raise PermissionError("order does not belong to the signed-in customer")
        return order

    def track_order(self, slots: dict, customer_id: str) -> dict:
        o = self._owned_order(customer_id, slots["order_id"])
        return {
            "order_id": o.id,
            "status": o.status.value,
            "placed_on": _date(o.placed_at),
            "items": [
                {"product": i.product_name, "size_us": str(i.size_us), "width": i.width.value,
                 "color": i.color, "quantity": i.quantity, "item_status": i.status.value}
                for i in o.items
            ],
            "total": money(self.s, o.total),
            "carrier": o.carrier,
            "tracking_number": o.tracking_number,
            "tracking_url": o.tracking_url,
            "estimated_delivery": _date(o.estimated_delivery_date),
            "delivered_on": _date(o.delivered_at),
            "shipping_to_city": o.shipping_city,
        }

    def refund_status(self, slots: dict, customer_id: str) -> dict:
        o = self._owned_order(customer_id, slots["order_id"])
        return {
            "order_id": o.id,
            "refund_timing": self.policies.get("refund_timing"),
            "returns": [
                {"return_id": r.id, "type": r.type.value, "status": r.status.value,
                 "refund_status": r.refund_status.value,
                 "refund_amount": money(self.s, r.refund_amount) if r.refund_amount is not None else None,
                 "refunded_on": _date(r.refunded_at), "requested_on": _date(r.created_at)}
                for r in o.returns
            ],
        }

    # --- writes (after CONFIRM + customer confirmation) --------------------

    def create_return(self, slots: dict, customer_id: str, session_id: str, now: datetime,
                      idempotency_key: str) -> dict:
        order = self._owned_order(customer_id, slots["order_id"])
        item = next(i for i in order.items if i.id == slots["order_item_id"])
        amount = refund_estimate(order, item)
        created = self.commerce.create_return(
            customer_id, order.id, item.id, slots["return_reason"], amount,
            idempotency_key=idempotency_key, chat_session_id=session_id, now=now)
        return {"return_id": created.return_id, "label_url": created.label_url,
                "refund_estimate": money(self.s, amount), "refund_timing": self.policies.get("refund_timing")}

    def create_exchange(self, slots: dict, customer_id: str, session_id: str, now: datetime,
                        target_variant_id: str, target: dict, idempotency_key: str) -> dict:
        created = self.commerce.create_exchange(
            customer_id, slots["order_id"], slots["order_item_id"], target_variant_id, slots.get("return_reason"),
            idempotency_key=idempotency_key, chat_session_id=session_id, now=now)
        return {"return_id": created.return_id, "label_url": created.label_url, "to_size": target["to_size"],
                "to_width": target["to_width"], "to_color": target["to_color"]}

    def cancel_order(self, slots: dict, customer_id: str, now: datetime, idempotency_key: str) -> dict:
        cancelled = self.commerce.cancel_order(customer_id, slots["order_id"], idempotency_key=idempotency_key, now=now)
        return {"order_id": cancelled.order_id, "total": money(self.s, cancelled.total)}
