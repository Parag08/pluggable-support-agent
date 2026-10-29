"""Commerce data the agent's handlers read and write: catalog, customers, orders, returns."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, Money, UTCDateTime, str_enum, utcnow


from app.core.enums import (
    Gender,
    Width,
    FitNote,
    OrderStatus,
    OrderItemStatus,
    ReturnType,
    ReturnStatus,
    ReturnReason,
    RefundStatus,
    ActorType,
    IdentityStatus,
    SessionStatus,
    MessageRole,
    IntentType,
    PipelineStage,
    StepStatus,
    PolicyOutcome,
    TurnOutcome,
    LLMPurpose,
    ParkedIntentStatus,
    ParkReason,
    AuthEventType,
    EscalationReason,
    EscalationPriority,
    EscalationStatus,
    FeedbackRating,
    EvalTarget,
    EvalType,
)  # noqa: F401  (re-exported from app.db.models)


# --- Catalog ----------------------------------------------------------------


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    handle: Mapped[str] = mapped_column(String(80), unique=True)
    sku_prefix: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), index=True)
    gender: Mapped[Gender] = mapped_column(str_enum(Gender))
    description: Mapped[str] = mapped_column(Text)
    materials: Mapped[list] = mapped_column(JSONType)
    features: Mapped[list] = mapped_column(JSONType)
    care_instructions: Mapped[str] = mapped_column(Text)
    fit_note: Mapped[FitNote] = mapped_column(str_enum(FitNote))
    fit_note_detail: Mapped[str] = mapped_column(Text)
    # Recommended adjustment vs. the customer's usual size (e.g. +0.5 = size up half).
    fit_size_offset: Mapped[Decimal] = mapped_column(Numeric(3, 1))
    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    is_final_sale: Mapped[bool] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product")


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (UniqueConstraint("product_id", "color", "size_us", "width"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True)
    color: Mapped[str] = mapped_column(String(40))
    size_us: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    width: Mapped[Width] = mapped_column(str_enum(Width))
    stock_qty: Mapped[int] = mapped_column(Integer)
    restock_date: Mapped[date | None] = mapped_column(Date)

    product: Mapped[Product] = relationship(back_populates="variants")


class SizeChart(Base):
    """US/UK/EU/foot-length conversion. Unisex products use the men's chart."""

    __tablename__ = "size_chart"
    __table_args__ = (UniqueConstraint("gender", "us"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gender: Mapped[Gender] = mapped_column(str_enum(Gender))
    us: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    uk: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    eu: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    foot_length_cm: Mapped[Decimal] = mapped_column(Numeric(4, 1))


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    address_line: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(80), index=True)
    region: Mapped[str] = mapped_column(String(80))
    postal_code: Mapped[str] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2))
    phone: Mapped[str] = mapped_column(String(30))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    opening_hours: Mapped[dict] = mapped_column(JSONType)
    offers_try_on: Mapped[bool] = mapped_column(Boolean)


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))
    percent_off: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    amount_off: Mapped[Decimal | None] = mapped_column(Money)
    min_subtotal: Mapped[Decimal | None] = mapped_column(Money)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean)


# --- Customers & orders -----------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    # OIDC `sub` from the identity server. NULL for guest-checkout customers.
    identity_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(30))
    is_guest: Mapped[bool] = mapped_column(Boolean)
    default_shipping_address: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_customer_placed", "customer_id", "placed_at"),)

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. SS10482
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(Money)
    discount_total: Mapped[Decimal] = mapped_column(Money)
    shipping_fee: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    discount_code: Mapped[str | None] = mapped_column(ForeignKey("discount_codes.code"))
    shipping_address: Mapped[dict] = mapped_column(JSONType)  # snapshot at checkout
    carrier: Mapped[str | None] = mapped_column(String(40))
    tracking_number: Mapped[str | None] = mapped_column(String(60))
    tracking_url: Mapped[str | None] = mapped_column(String(255))
    estimated_delivery_date: Mapped[date | None] = mapped_column(Date)
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    fulfilled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    shipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    returns: Mapped[list["ReturnRequest"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"))
    # Snapshots at purchase time — catalog may change later.
    product_name: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str] = mapped_column(String(40))
    color: Mapped[str] = mapped_column(String(40))
    size_us: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    width: Mapped[Width] = mapped_column(str_enum(Width))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    is_final_sale: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[OrderItemStatus] = mapped_column(str_enum(OrderItemStatus))

    order: Mapped[Order] = relationship(back_populates="items")
    variant: Mapped[ProductVariant] = relationship()


class ReturnRequest(Base):
    __tablename__ = "returns"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. RMA1001
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    type: Mapped[ReturnType] = mapped_column(str_enum(ReturnType))
    status: Mapped[ReturnStatus] = mapped_column(str_enum(ReturnStatus))
    reason: Mapped[ReturnReason] = mapped_column(str_enum(ReturnReason))
    reason_detail: Mapped[str | None] = mapped_column(Text)
    refund_amount: Mapped[Decimal | None] = mapped_column(Money)
    refund_status: Mapped[RefundStatus] = mapped_column(str_enum(RefundStatus))
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    label_url: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[ActorType] = mapped_column(str_enum(ActorType))
    # Set when the agent created this return — links the outcome back to the conversation.
    chat_session_id: Mapped[str | None] = mapped_column(ForeignKey("chat_sessions.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    order: Mapped[Order] = relationship(back_populates="returns")
    items: Mapped[list["ReturnItem"]] = relationship(back_populates="return_request")


class ReturnItem(Base):
    __tablename__ = "return_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    return_id: Mapped[str] = mapped_column(ForeignKey("returns.id"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    # For exchanges: the variant the customer is swapping to.
    exchange_variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id"))

    return_request: Mapped[ReturnRequest] = relationship(back_populates="items")


class AgentWrite(Base):
    """Idempotency log for writes made through the SQL reference adapter.

    A repeated key returns the stored result instead of writing again; the same key with
    different request details is refused.
    """

    __tablename__ = "agent_writes"

    idempotency_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
