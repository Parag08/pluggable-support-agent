"""Reference CommercePort adapter over the demo SQL schema (`app/db/models/commerce.py`).

The agent never receives a database session: it gets this object, and can do exactly what the
public methods allow (the allowlist is pinned in `tests/test_agent_boundary.py`).

Rules enforced here, independently of the agent's policy engine:
- Customer data is always scoped by `customer_id`: another customer's order is
  indistinguishable from one that doesn't exist.
- Writes re-check ownership and state, and are all-or-nothing.
- Writes are idempotent: a repeated `idempotency_key` returns the stored result.
- There is no delete, no refund, no discount, no price/address/customer change,
  and no way to grant identity (that lives in `app.auth`).
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.enums import (
    ActorType,
    Gender,
    OrderItemStatus,
    OrderStatus,
    RefundStatus,
    ReturnReason,
    ReturnStatus,
    ReturnType,
)
from app.core.types import (
    CustomerProfile,
    DataAccessError,
    DiscountInfo,
    OrderCancelled,
    OrderInfo,
    OrderItemInfo,
    OrderSummary,
    ProductInfo,
    ReturnCreated,
    ReturnInfo,
    StoreInfo,
    VariantInfo,
)
from app.db.models import (
    AgentWrite,
    Customer,
    DiscountCode,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    ReturnItem,
    ReturnRequest,
    SizeChart,
    Store,
)
from app.ports.commerce import ALL_CAPABILITIES

CANCELLABLE = (OrderStatus.PENDING, OrderStatus.PAID)


def _variant(v: ProductVariant) -> VariantInfo:
    return VariantInfo(str(v.id), v.product_id, v.sku, v.color, v.size_us, v.width, v.stock_qty, v.restock_date)


def _product(p: Product) -> ProductInfo:
    return ProductInfo(
        id=p.id, handle=p.handle, name=p.name, category=p.category, gender=p.gender,
        description=p.description, materials=tuple(p.materials), features=tuple(p.features),
        care_instructions=p.care_instructions, fit_note_detail=p.fit_note_detail,
        fit_size_offset=p.fit_size_offset, price=p.price, is_final_sale=p.is_final_sale,
        variants=tuple(_variant(v) for v in p.variants),
    )


def _order(o: Order) -> OrderInfo:
    return OrderInfo(
        id=o.id, status=o.status, placed_at=o.placed_at, subtotal=o.subtotal,
        discount_total=o.discount_total, total=o.total, carrier=o.carrier,
        tracking_number=o.tracking_number, tracking_url=o.tracking_url,
        estimated_delivery_date=o.estimated_delivery_date, delivered_at=o.delivered_at,
        shipping_city=(o.shipping_address or {}).get("city"),
        items=tuple(
            OrderItemInfo(str(i.id), i.variant.product_id, str(i.variant_id), i.product_name, i.sku, i.color,
                          i.size_us, i.width, i.quantity, i.unit_price, i.is_final_sale, i.status)
            for i in o.items
        ),
        returns=tuple(
            ReturnInfo(r.id, r.type, r.status, r.refund_status, r.refund_amount, r.refunded_at, r.created_at)
            for r in o.returns
        ),
    )


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


class SqlReferenceCommerce:
    def __init__(self, db: Session, settings: Settings):
        self.__db = db  # name-mangled: not part of the agent's surface
        self.__s = settings

    def capabilities(self) -> frozenset[str]:
        return ALL_CAPABILITIES

    # ------------------------------------------------------------------
    # Catalog (read-only, public)
    # ------------------------------------------------------------------

    def list_products(self) -> list[ProductInfo]:
        return [_product(p) for p in self.__db.scalars(select(Product).where(Product.is_active.is_(True)))]

    def get_product(self, product_id: str) -> ProductInfo | None:
        p = self.__db.get(Product, product_id)
        return _product(p) if p is not None and p.is_active else None

    def find_variant(self, product_id: str, size_us: Decimal, width: str, color: str) -> VariantInfo | None:
        v = self.__db.scalar(select(ProductVariant).where(
            ProductVariant.product_id == product_id,
            ProductVariant.size_us == size_us,
            ProductVariant.width == width,
            func.lower(ProductVariant.color) == color.lower(),
        ))
        return _variant(v) if v is not None else None

    def convert_to_us_size(self, gender: Gender, system: str, value: Decimal) -> Decimal | None:
        """Nearest US size on the size chart for a UK/EU/CM value."""
        column = {"UK": SizeChart.uk, "EU": SizeChart.eu, "CM": SizeChart.foot_length_cm}.get(system)
        if column is None:
            return None
        row = self.__db.scalars(
            select(SizeChart).where(SizeChart.gender == gender).order_by(func.abs(column - value)).limit(1)
        ).first()
        return row.us if row is not None else None

    def list_stores(self) -> list[StoreInfo]:
        return [
            StoreInfo(s.name, s.address_line, s.city, s.region, s.postal_code, s.phone,
                      dict(s.opening_hours), s.offers_try_on)
            for s in self.__db.scalars(select(Store))
        ]

    def get_discount_code(self, code: str) -> DiscountInfo | None:
        d = self.__db.get(DiscountCode, code)
        if d is None:
            return None
        return DiscountInfo(d.code, d.description, d.is_active, d.starts_at, d.ends_at, d.min_subtotal)

    # ------------------------------------------------------------------
    # Customer data (read-only, always scoped to customer_id)
    # ------------------------------------------------------------------

    def __owned_order(self, customer_id: str, order_id: str) -> Order | None:
        order = self.__db.get(Order, order_id)
        return order if order is not None and order.customer_id == customer_id else None

    def get_customer_profile(self, customer_id: str) -> CustomerProfile | None:
        c = self.__db.get(Customer, customer_id)
        return CustomerProfile(c.id, c.first_name, c.email) if c is not None else None

    def get_customer_order(self, customer_id: str, order_id: str) -> OrderInfo | None:
        """None if the order doesn't exist *or* belongs to someone else."""
        order = self.__owned_order(customer_id, order_id)
        return _order(order) if order is not None else None

    def list_customer_orders(self, customer_id: str, limit: int) -> list[OrderSummary]:
        rows = self.__db.scalars(
            select(Order).where(Order.customer_id == customer_id).order_by(Order.placed_at.desc()).limit(limit)
        )
        return [OrderSummary(o.id, o.placed_at, o.status) for o in rows]

    def count_item_exchanges(self, customer_id: str, order_item_id: str) -> int:
        return self.__db.scalar(
            select(func.count()).select_from(ReturnItem)
            .join(ReturnRequest, ReturnRequest.id == ReturnItem.return_id)
            .where(ReturnItem.order_item_id == int(order_item_id), ReturnRequest.customer_id == customer_id,
                   ReturnRequest.type == ReturnType.EXCHANGE)
        )

    # ------------------------------------------------------------------
    # Customer writes (only after policy CONFIRM + customer confirmation)
    # ------------------------------------------------------------------

    def __replay(self, key: str, kind: str, fingerprint: str) -> dict | None:
        """The stored result of an earlier write with this key, if any."""
        row = self.__db.get(AgentWrite, key)
        if row is None:
            return None
        if row.kind != kind or row.fingerprint != fingerprint:
            raise DataAccessError("idempotency key was already used for a different request")
        return row.result

    def __record(self, key: str, kind: str, fingerprint: str, result: dict, now: datetime) -> None:
        self.__db.add(AgentWrite(idempotency_key=key, kind=kind, fingerprint=fingerprint, result=result,
                                 created_at=now))
        self.__db.flush()

    def __returnable_item(self, customer_id: str, order_id: str, order_item_id: str) -> tuple[Order, OrderItem]:
        order = self.__owned_order(customer_id, order_id)
        if order is None:
            raise DataAccessError("order not found for this customer")
        item = next((i for i in order.items if str(i.id) == order_item_id), None)
        if item is None:
            raise DataAccessError("item is not part of this order")
        if order.status != OrderStatus.DELIVERED or item.is_final_sale or item.status != OrderItemStatus.ACTIVE:
            raise DataAccessError("item is not returnable")
        return order, item

    def __next_return_id(self) -> str:
        prefix = self.__s.return_id_prefix
        ids = self.__db.scalars(select(ReturnRequest.id).where(ReturnRequest.id.like(f"{prefix}%"))).all()
        numbers = [int(i[len(prefix):]) for i in ids if i[len(prefix):].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1}"

    def create_return(
        self, customer_id: str, order_id: str, order_item_id: str, reason: str, refund_amount: Decimal,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated:
        fp = _fingerprint("return", customer_id, order_id, order_item_id, reason, refund_amount)
        with self.__db.begin_nested():
            done = self.__replay(idempotency_key, "return", fp)
            if done is not None:
                return ReturnCreated(done["return_id"], done["label_url"])
            order, item = self.__returnable_item(customer_id, order_id, order_item_id)
            rid = self.__next_return_id()
            label = self.__s.return_label_url_template.format(return_id=rid)
            ret = ReturnRequest(
                id=rid, order=order, customer_id=customer_id, type=ReturnType.RETURN,
                status=ReturnStatus.LABEL_SENT, reason=ReturnReason(reason), refund_amount=refund_amount,
                refund_status=RefundStatus.PENDING, label_url=label, created_by=ActorType.AGENT,
                chat_session_id=chat_session_id, created_at=now, updated_at=now,
            )
            self.__db.add(ret)
            self.__db.add(ReturnItem(return_request=ret, order_item_id=item.id, quantity=item.quantity))
            item.status = OrderItemStatus.RETURN_REQUESTED
            self.__record(idempotency_key, "return", fp, {"return_id": rid, "label_url": label}, now)
        return ReturnCreated(rid, label)

    def create_exchange(
        self, customer_id: str, order_id: str, order_item_id: str, target_variant_id: str, reason: str | None,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated:
        fp = _fingerprint("exchange", customer_id, order_id, order_item_id, target_variant_id, reason)
        with self.__db.begin_nested():
            done = self.__replay(idempotency_key, "exchange", fp)
            if done is not None:
                return ReturnCreated(done["return_id"], done["label_url"])
            order, item = self.__returnable_item(customer_id, order_id, order_item_id)
            variant = self.__db.get(ProductVariant, int(target_variant_id))
            if variant is None or variant.product_id != item.variant.product_id or variant.id == item.variant_id:
                raise DataAccessError("invalid exchange target")
            if variant.stock_qty < item.quantity:
                raise DataAccessError("exchange target out of stock")
            rid = self.__next_return_id()
            label = self.__s.return_label_url_template.format(return_id=rid)
            ret = ReturnRequest(
                id=rid, order=order, customer_id=customer_id, type=ReturnType.EXCHANGE,
                status=ReturnStatus.LABEL_SENT, reason=ReturnReason(reason) if reason else ReturnReason.OTHER,
                refund_amount=None, refund_status=RefundStatus.NOT_APPLICABLE, label_url=label,
                created_by=ActorType.AGENT, chat_session_id=chat_session_id, created_at=now, updated_at=now,
            )
            self.__db.add(ret)
            self.__db.add(ReturnItem(return_request=ret, order_item_id=item.id, quantity=item.quantity,
                                     exchange_variant_id=variant.id))
            variant.stock_qty -= item.quantity  # reserve the replacement
            item.status = OrderItemStatus.EXCHANGE_REQUESTED
            self.__record(idempotency_key, "exchange", fp, {"return_id": rid, "label_url": label}, now)
        return ReturnCreated(rid, label)

    def cancel_order(
        self, customer_id: str, order_id: str, *, idempotency_key: str, now: datetime,
    ) -> OrderCancelled:
        fp = _fingerprint("cancel", customer_id, order_id)
        with self.__db.begin_nested():
            done = self.__replay(idempotency_key, "cancel", fp)
            if done is not None:
                return OrderCancelled(done["order_id"], Decimal(done["total"]))
            order = self.__owned_order(customer_id, order_id)
            if order is None:
                raise DataAccessError("order not found for this customer")
            if order.status not in CANCELLABLE:
                raise DataAccessError(f"order in status {order.status.value} can't be cancelled")
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = now
            for item in order.items:
                item.status = OrderItemStatus.CANCELLED
            self.__record(idempotency_key, "cancel", fp, {"order_id": order.id, "total": str(order.total)}, now)
        return OrderCancelled(order.id, order.total)
