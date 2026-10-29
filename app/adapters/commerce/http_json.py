"""CommercePort over JSON-over-HTTP: connect any company system, written in any language.

The company exposes one endpoint per CommercePort function:

    POST {COMMERCE_HTTP_BASE_URL}/{function_name}
    Authorization: Bearer {COMMERCE_HTTP_TOKEN}
    Idempotency-Key: <key>          (writes only; also sent in the body)
    body:    {"<argument>": <value>, ...}          e.g. {"customer_id": "c1", "order_id": "SS1"}
    200:     {"result": <value or null>}            null = not found (or not this customer's)
    409/422: {"error": "<why it was refused>"}      → the write was refused; nothing was changed
    other:   treated as "unavailable" (the agent retries writes safely thanks to the idempotency key)

Function names, arguments and result shapes are the ones in `app/ports/commerce.py` and
`app/core/types.py` (the value encoding is in `codec.py`). `capabilities` returns a list of
capability names; leave out what the company doesn't support and the agent turns those
intents off. `http_json_server.py` is a reference server (also used by the conformance tests).
"""

import typing
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.adapters.commerce.codec import decode, encode
from app.config import Settings
from app.core.enums import Gender
from app.core.types import (
    CustomerProfile,
    DataAccessError,
    DiscountInfo,
    OrderCancelled,
    OrderInfo,
    OrderSummary,
    ProductInfo,
    ReturnCreated,
    StoreInfo,
    VariantInfo,
)
from app.ports.commerce import CommercePort


class CommerceUnavailable(Exception):
    """The company's system didn't give a usable answer (network error, 5xx, malformed response)."""


class HttpJsonCommerce:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if client is None:
            if not settings.commerce_http_base_url:
                raise RuntimeError("COMMERCE_PROVIDER=http_json needs COMMERCE_HTTP_BASE_URL")
            token = settings.commerce_http_token.get_secret_value()
            client = httpx.Client(
                base_url=settings.commerce_http_base_url.rstrip("/") + "/",
                timeout=settings.commerce_http_timeout_seconds,
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        self._client = client
        self._capabilities: frozenset[str] | None = None

    def _call(self, function: str, **args: Any) -> Any:
        headers = {"Idempotency-Key": args["idempotency_key"]} if "idempotency_key" in args else {}
        try:
            resp = self._client.post(function, json={k: encode(v) for k, v in args.items()}, headers=headers)
        except httpx.HTTPError as exc:
            raise CommerceUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code in (409, 422):
            raise DataAccessError(resp.json().get("error", "refused"))
        if resp.status_code != 200:
            raise CommerceUnavailable(f"{function} returned HTTP {resp.status_code}")
        try:
            result = resp.json()["result"]
        except (ValueError, KeyError) as exc:
            raise CommerceUnavailable(f"{function} returned a malformed response") from exc
        returns = typing.get_type_hints(getattr(CommercePort, function))["return"]
        return decode(returns, result)

    def capabilities(self) -> frozenset[str]:
        if self._capabilities is None:
            self._capabilities = self._call("capabilities")
        return self._capabilities

    # --- catalog ---
    def list_products(self) -> list[ProductInfo]:
        return self._call("list_products")

    def get_product(self, product_id: str) -> ProductInfo | None:
        return self._call("get_product", product_id=product_id)

    def find_variant(self, product_id: str, size_us: Decimal, width: str, color: str) -> VariantInfo | None:
        return self._call("find_variant", product_id=product_id, size_us=size_us, width=width, color=color)

    def convert_to_us_size(self, gender: Gender, system: str, value: Decimal) -> Decimal | None:
        return self._call("convert_to_us_size", gender=gender, system=system, value=value)

    def list_stores(self) -> list[StoreInfo]:
        return self._call("list_stores")

    def get_discount_code(self, code: str) -> DiscountInfo | None:
        return self._call("get_discount_code", code=code)

    # --- customer data ---
    def get_customer_profile(self, customer_id: str) -> CustomerProfile | None:
        return self._call("get_customer_profile", customer_id=customer_id)

    def get_customer_order(self, customer_id: str, order_id: str) -> OrderInfo | None:
        return self._call("get_customer_order", customer_id=customer_id, order_id=order_id)

    def list_customer_orders(self, customer_id: str, limit: int) -> list[OrderSummary]:
        return self._call("list_customer_orders", customer_id=customer_id, limit=limit)

    def count_item_exchanges(self, customer_id: str, order_item_id: str) -> int:
        return self._call("count_item_exchanges", customer_id=customer_id, order_item_id=order_item_id)

    # --- customer writes ---
    def create_return(
        self, customer_id: str, order_id: str, order_item_id: str, reason: str, refund_amount: Decimal,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated:
        return self._call("create_return", customer_id=customer_id, order_id=order_id, order_item_id=order_item_id,
                          reason=reason, refund_amount=refund_amount, idempotency_key=idempotency_key,
                          chat_session_id=chat_session_id, now=now)

    def create_exchange(
        self, customer_id: str, order_id: str, order_item_id: str, target_variant_id: str, reason: str | None,
        *, idempotency_key: str, chat_session_id: str, now: datetime,
    ) -> ReturnCreated:
        return self._call("create_exchange", customer_id=customer_id, order_id=order_id,
                          order_item_id=order_item_id, target_variant_id=target_variant_id, reason=reason,
                          idempotency_key=idempotency_key, chat_session_id=chat_session_id, now=now)

    def cancel_order(
        self, customer_id: str, order_id: str, *, idempotency_key: str, now: datetime,
    ) -> OrderCancelled:
        return self._call("cancel_order", customer_id=customer_id, order_id=order_id,
                          idempotency_key=idempotency_key, now=now)
