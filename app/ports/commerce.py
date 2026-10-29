"""CommercePort: the company's catalog, customers, orders and returns.

Rules every adapter must follow (enforced by `tests/conformance/`):
- Customer data is always scoped by `customer_id`. Another customer's order must be
  indistinguishable from one that doesn't exist (`None`).
- Results are plain frozen values (`app.core.types`), never ORM objects or API clients.
- Writes take an `idempotency_key`. A repeated key returns the original result and
  changes nothing; the same key with different details is refused.
- Writes re-check ownership and state themselves, and are all-or-nothing.
- `capabilities()` declares what the adapter supports, so the agent turns the rest off.
- There is no delete, refund, discount, price, address or identity change on this port.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.core.enums import Gender
from app.core.types import (
    CustomerProfile,
    DiscountInfo,
    OrderCancelled,
    OrderInfo,
    OrderSummary,
    ProductInfo,
    ReturnCreated,
    StoreInfo,
    VariantInfo,
)

# The capability vocabulary. Each intent in agent_config/intents.json lists the ones it `requires`.
CATALOG_READ = "catalog.read"
SIZE_CHART_READ = "size_chart.read"
STORES_READ = "stores.read"
DISCOUNTS_READ = "discounts.read"
ORDERS_READ = "orders.read"
RETURNS_WRITE = "returns.write"
EXCHANGES_WRITE = "exchanges.write"
ORDERS_CANCEL = "orders.cancel"

ALL_CAPABILITIES = frozenset({
    CATALOG_READ, SIZE_CHART_READ, STORES_READ, DISCOUNTS_READ,
    ORDERS_READ, RETURNS_WRITE, EXCHANGES_WRITE, ORDERS_CANCEL,
})


class CommercePort(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    # --- catalog (read-only, public) ---
    def list_products(self) -> list[ProductInfo]: ...
    def get_product(self, product_id: str) -> ProductInfo | None: ...
    def find_variant(self, product_id: str, size_us: Decimal, width: str, color: str) -> VariantInfo | None: ...
    def convert_to_us_size(self, gender: Gender, system: str, value: Decimal) -> Decimal | None: ...
    def list_stores(self) -> list[StoreInfo]: ...
    def get_discount_code(self, code: str) -> DiscountInfo | None: ...

    # --- customer data (read-only, always scoped to customer_id) ---
    def get_customer_profile(self, customer_id: str) -> CustomerProfile | None: ...
    def get_customer_order(self, customer_id: str, order_id: str) -> OrderInfo | None: ...
    def list_customer_orders(self, customer_id: str, limit: int) -> list[OrderSummary]: ...
    def count_item_exchanges(self, customer_id: str, order_item_id: str) -> int: ...

    # --- customer writes (only after policy CONFIRM + the customer's confirmation) ---
    def create_return(
        self, customer_id: str, order_id: str, order_item_id: str, reason: str, refund_amount: Decimal,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated: ...

    def create_exchange(
        self, customer_id: str, order_id: str, order_item_id: str, target_variant_id: str, reason: str | None,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated: ...

    def cancel_order(
        self, customer_id: str, order_id: str, *, idempotency_key: str, now: datetime,
    ) -> OrderCancelled: ...
