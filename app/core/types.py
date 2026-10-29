"""Plain values exchanged through the ports (commerce, conversation store).

These are frozen copies, not ORM objects: changing one can't write to the
database, and they can't lazy-load anything. The only way the agent changes
data is by calling a gateway function.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.enums import (
    Gender,
    IdentityStatus,
    OrderItemStatus,
    OrderStatus,
    RefundStatus,
    ReturnStatus,
    ReturnType,
    Width,
)


@dataclass(frozen=True)
class VariantInfo:
    id: str  # opaque: whatever the company's system uses
    product_id: str
    sku: str
    color: str
    size_us: Decimal
    width: Width
    stock_qty: int
    restock_date: date | None


@dataclass(frozen=True)
class ProductInfo:
    id: str
    handle: str
    name: str
    category: str
    gender: Gender
    description: str
    materials: tuple[str, ...]
    features: tuple[str, ...]
    care_instructions: str
    fit_note_detail: str
    fit_size_offset: Decimal
    price: Decimal
    is_final_sale: bool
    variants: tuple[VariantInfo, ...]

    @property
    def colors(self) -> list[str]:
        return sorted({v.color for v in self.variants})

    @property
    def widths(self) -> list[str]:
        return sorted({v.width.value for v in self.variants})


@dataclass(frozen=True)
class StoreInfo:
    name: str
    address_line: str
    city: str
    region: str
    postal_code: str
    phone: str
    opening_hours: dict[str, str]
    offers_try_on: bool


@dataclass(frozen=True)
class DiscountInfo:
    code: str
    description: str
    is_active: bool
    starts_at: datetime | None
    ends_at: datetime | None
    min_subtotal: Decimal | None


@dataclass(frozen=True)
class CustomerProfile:
    id: str
    first_name: str
    email: str


@dataclass(frozen=True)
class OrderItemInfo:
    id: str  # opaque: whatever the company's system uses
    product_id: str
    variant_id: str
    product_name: str
    sku: str
    color: str
    size_us: Decimal
    width: Width
    quantity: int
    unit_price: Decimal
    is_final_sale: bool
    status: OrderItemStatus


@dataclass(frozen=True)
class ReturnInfo:
    id: str
    type: ReturnType
    status: ReturnStatus
    refund_status: RefundStatus
    refund_amount: Decimal | None
    refunded_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class OrderInfo:
    id: str
    status: OrderStatus
    placed_at: datetime
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal
    carrier: str | None
    tracking_number: str | None
    tracking_url: str | None
    estimated_delivery_date: date | None
    delivered_at: datetime | None
    shipping_city: str | None
    items: tuple[OrderItemInfo, ...]
    returns: tuple[ReturnInfo, ...]


@dataclass(frozen=True)
class OrderSummary:
    id: str
    placed_at: datetime
    status: OrderStatus


@dataclass(frozen=True)
class SessionInfo:
    id: str
    identity_status: IdentityStatus
    customer_id: str | None
    identity_expires_at: datetime | None
    last_activity_at: datetime
    state: dict[str, Any] = field(default_factory=dict)  # a copy; persist with save_session_state()


@dataclass(frozen=True)
class MessageRef:
    id: int
    seq: int


@dataclass(frozen=True)
class ChatLine:
    role: str
    content: str


@dataclass(frozen=True)
class ParkedRequest:
    id: int
    intent: str
    slots: dict[str, Any]


@dataclass(frozen=True)
class ReturnCreated:
    return_id: str
    label_url: str


@dataclass(frozen=True)
class OrderCancelled:
    order_id: str
    total: Decimal


class DataAccessError(Exception):
    """A port call was refused (wrong owner, invalid state, unsupported). Never partially applied."""
