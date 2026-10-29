"""Stage 3: deterministic policy and authorization. No LLM is involved.

Order of checks: escalation intents → identity (protected) → ownership →
business rules → write gate (CONFIRM). Every check is recorded so the trace
shows exactly why a decision was made.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.agent.config_loader import IntentSpec
from app.agent.slots import Validation
from app.config import Settings
from app.core.types import OrderInfo, OrderItemInfo
from app.ports.commerce import CommercePort
from app.db.models import EscalationReason, OrderItemStatus, OrderStatus, PolicyOutcome


@dataclass
class PolicyDecision:
    outcome: PolicyOutcome
    reason_code: str
    params: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    target_variant_id: str | None = None

    def to_log(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "params": self.params,
            "checks": self.checks,
            "escalation_reason": self.escalation_reason.value if self.escalation_reason else None,
            "target_variant_id": self.target_variant_id,
        }


def money(settings: Settings, amount: Decimal) -> str:
    return f"{settings.currency_symbol}{amount:.2f}"


def refund_estimate(order: OrderInfo, item: OrderItemInfo) -> Decimal:
    ratio = (order.discount_total / order.subtotal) if order.subtotal else Decimal(0)
    return (item.unit_price * item.quantity * (1 - ratio)).quantize(Decimal("0.01"))


class PolicyEngine:
    def __init__(self, commerce: CommercePort, settings: Settings):
        self.commerce = commerce
        self.s = settings
        self.capabilities = commerce.capabilities()

    def missing_capabilities(self, spec: IntentSpec) -> list[str]:
        """Capabilities this intent needs that the company's adapter doesn't offer."""
        return sorted(set(spec.requires) - self.capabilities)

    def decide(
        self,
        spec: IntentSpec,
        v: Validation,
        *,
        identity_valid: bool,
        customer_id: str | None,
        now: datetime,
    ) -> PolicyDecision:
        checks: list[dict] = []

        def check(name: str, passed: bool, **detail) -> bool:
            checks.append({"check": name, "passed": passed, **detail})
            return passed

        def result(outcome: PolicyOutcome, code: str, **kw) -> PolicyDecision:
            return PolicyDecision(outcome, code, checks=checks, **kw)

        if spec.kind == "escalate":
            check("intent_requires_human", True)
            return result(PolicyOutcome.ESCALATE, spec.escalation_reason.value.upper(), escalation_reason=spec.escalation_reason)

        if spec.protected:
            if not check("identity_validated", identity_valid):
                return result(PolicyOutcome.AUTH_REQUIRED, "IDENTITY_REQUIRED")
            if "order_id" in v.slots:
                owned = self.commerce.get_customer_order(customer_id, v.slots["order_id"]) is not None
                if not check("order_belongs_to_customer", owned):
                    return result(PolicyOutcome.DENY, "ORDER_NOT_FOUND", params={"order_id": v.slots["order_id"]})

        if spec.kind == "answer":
            return result(PolicyOutcome.ALLOW, "OK")
        if spec.kind == "decline":
            return result(PolicyOutcome.DENY, "OUT_OF_SCOPE")

        order = v.order
        if spec.name == "cancel_or_modify_order":
            return self._cancel(order, v, check, result)

        item = v.item
        params = {"order_id": order.id, "return_window_days": self.s.return_window_days}
        if item is None:
            check("item_in_order", False)
            return result(PolicyOutcome.DENY, "ITEM_NOT_FOUND", params=params)
        params["product_name"] = item.product_name

        denial = self._return_eligibility(order, item, now, spec.name, check, customer_id)
        if denial:
            if denial == "RETURN_WINDOW_EXPIRED":
                params["delivered_on"] = f"{order.delivered_at:%B %d, %Y}"
            return result(PolicyOutcome.DENY, denial, params=params)

        if spec.name == "return_request":
            params.update(
                quantity=item.quantity, size_us=str(item.size_us), width=item.width.value, color=item.color,
                refund_estimate=money(self.s, refund_estimate(order, item)),
            )
            return result(PolicyOutcome.CONFIRM, "RETURN_ELIGIBLE", params=params)

        # exchange_request
        target = v.slots["exchange_target"]
        same = (Decimal(target["size_us"]) == item.size_us and target["width"] == item.width.value
                and target["color"].lower() == item.color.lower())
        if not check("exchange_differs_from_current", not same):
            return result(PolicyOutcome.DENY, "SAME_VARIANT", params=params)
        variant = self.commerce.find_variant(item.product_id, Decimal(target["size_us"]), target["width"], target["color"])
        if not check("target_variant_exists", variant is not None, target=target):
            return result(PolicyOutcome.DENY, "VARIANT_NOT_AVAILABLE", params=params)
        if not check("target_in_stock", variant.stock_qty >= item.quantity, stock=variant.stock_qty):
            note = f" (expected back around {variant.restock_date:%B %d})" if variant.restock_date else ""
            return result(PolicyOutcome.DENY, "OUT_OF_STOCK", params={**params, "restock_note": note})
        params.update(
            from_size=str(item.size_us), from_width=item.width.value, from_color=item.color,
            to_size=str(variant.size_us), to_width=variant.width.value, to_color=variant.color,
        )
        return result(PolicyOutcome.CONFIRM, "EXCHANGE_ELIGIBLE", params=params, target_variant_id=variant.id)

    def _return_eligibility(self, order: OrderInfo, item: OrderItemInfo, now: datetime, intent: str, check,
                            customer_id: str) -> str | None:
        if not check("order_not_cancelled", order.status != OrderStatus.CANCELLED):
            return "ORDER_CANCELLED"
        if not check("order_delivered", order.status == OrderStatus.DELIVERED and order.delivered_at is not None):
            return "NOT_DELIVERED"
        if not check("item_not_final_sale", not item.is_final_sale):
            return "FINAL_SALE"
        days = (now - order.delivered_at).days
        if not check("within_return_window", days <= self.s.return_window_days,
                     days_since_delivery=days, window=self.s.return_window_days):
            return "RETURN_WINDOW_EXPIRED"
        if intent == "exchange_request":
            exchanges = self.commerce.count_item_exchanges(customer_id, item.id)
            if not check("exchange_limit", exchanges < self.s.max_exchanges_per_item,
                         exchanges=exchanges, limit=self.s.max_exchanges_per_item):
                return "EXCHANGE_LIMIT"
        if not check("item_has_no_open_return", item.status == OrderItemStatus.ACTIVE, item_status=item.status.value):
            return "ALREADY_RETURNED"
        return None

    def _cancel(self, order: OrderInfo, v: Validation, check, result) -> PolicyDecision:
        params = {"order_id": order.id, "return_window_days": self.s.return_window_days}
        if v.slots.get("change_type") != "cancel_order":
            check("change_type_automatable", False, change_type=v.slots.get("change_type"))
            return result(PolicyOutcome.ESCALATE, "MODIFICATION_NEEDS_HUMAN",
                          escalation_reason=EscalationReason.POLICY_EXCEPTION, params=params)
        if not check("order_not_cancelled", order.status != OrderStatus.CANCELLED):
            return result(PolicyOutcome.DENY, "ORDER_CANCELLED", params=params)
        if not check("order_not_shipped", order.status not in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)):
            return result(PolicyOutcome.DENY, "ALREADY_SHIPPED", params=params)
        if not check("order_before_fulfilment", order.status in (OrderStatus.PENDING, OrderStatus.PAID),
                     status=order.status.value):
            # Being packed right now — a person may still be able to stop it.
            return result(PolicyOutcome.ESCALATE, "IN_FULFILMENT",
                          escalation_reason=EscalationReason.POLICY_EXCEPTION, params=params)
        params.update(item_count=sum(i.quantity for i in order.items), total=money(self.s, order.total))
        return result(PolicyOutcome.CONFIRM, "CANCEL_ELIGIBLE", params=params)
