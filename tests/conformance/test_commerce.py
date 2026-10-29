"""What every CommercePort adapter must do (see app/ports/commerce.py)."""

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable

from app.core.enums import OrderItemStatus, OrderStatus
from app.core.types import DataAccessError
from app.db.base import utcnow
from app.db.models import ProductVariant
from app.ports.commerce import ALL_CAPABILITIES, ORDERS_READ

NOW = utcnow()


def write_args(env, key):
    return dict(idempotency_key=key, chat_session_id=env.session_id, now=NOW)


def maya_item(env, order="SS90001", index=0):
    return env.commerce.get_customer_order("cus_9001", order).items[index].id


def _is_orm_object(value) -> bool:
    try:
        sa_inspect(value)
        return True
    except NoInspectionAvailable:
        return False


# --- reads ----------------------------------------------------------------------


def test_capabilities_use_the_published_vocabulary(env):
    caps = env.commerce.capabilities()
    assert caps and caps <= ALL_CAPABILITIES and ORDERS_READ in caps


def test_results_are_frozen_plain_values_and_identical_across_adapters(env):
    order = env.commerce.get_customer_order("cus_9001", "SS90001")
    product = env.commerce.list_products()[0]
    for value in (order, order.items[0], product, product.variants[0], env.commerce.list_stores()[0]):
        assert dataclasses.is_dataclass(value) and not _is_orm_object(value)
    with pytest.raises(dataclasses.FrozenInstanceError):
        order.status = OrderStatus.CANCELLED  # type: ignore[misc]
    # Decimals, dates, enums and nested values survive the adapter unchanged.
    assert order == env.reference.get_customer_order("cus_9001", "SS90001")
    assert env.commerce.list_products() == env.reference.list_products()
    assert env.commerce.list_stores() == env.reference.list_stores()


def test_other_customers_orders_look_like_they_do_not_exist(env):
    assert env.commerce.get_customer_order("cus_9001", "SS90007") is None  # Daniel's order
    assert env.commerce.get_customer_order("cus_9001", "SS00000") is None  # doesn't exist
    assert all(o.id.startswith("SS9") for o in env.commerce.list_customer_orders("cus_9001", 50))
    assert env.commerce.count_item_exchanges("cus_9001", maya_item(env)) == 0


def test_size_conversion_and_unknown_lookups(env):
    product = next(p for p in env.commerce.list_products() if p.gender.value == "women")
    assert env.commerce.convert_to_us_size(product.gender, "UK", Decimal("5")) is not None
    assert env.commerce.convert_to_us_size(product.gender, "XX", Decimal("5")) is None
    assert env.commerce.get_product("nope") is None
    assert env.commerce.get_discount_code("NOPE") is None
    assert env.commerce.get_customer_profile("cus_0000") is None


# --- writes ---------------------------------------------------------------------


def test_writes_recheck_ownership_and_state_and_leave_nothing_behind(env):
    c = env.commerce
    item = maya_item(env)
    with pytest.raises(DataAccessError):  # wrong customer
        c.create_return("cus_9002", "SS90001", item, "too_small", Decimal("1"), **write_args(env, "k1"))
    with pytest.raises(DataAccessError):  # final-sale item
        sandal = next(i for i in c.get_customer_order("cus_9001", "SS90003").items if i.is_final_sale)
        c.create_return("cus_9001", "SS90003", sandal.id, "changed_mind", Decimal("1"), **write_args(env, "k2"))
    with pytest.raises(DataAccessError):  # not delivered yet
        c.create_return("cus_9001", "SS90004", maya_item(env, "SS90004"), "too_small", Decimal("1"),
                        **write_args(env, "k3"))
    with pytest.raises(DataAccessError):  # already shipped
        c.cancel_order("cus_9001", "SS90004", idempotency_key="k4", now=NOW)
    with pytest.raises(DataAccessError):  # someone else's order
        c.cancel_order("cus_9001", "SS90005", idempotency_key="k5", now=NOW)

    target = env.db.query(ProductVariant).filter_by(sku="CSRW-SAN-R-080").one()
    target.stock_qty = 0
    with pytest.raises(DataAccessError):  # out of stock
        c.create_exchange("cus_9001", "SS90001", item, str(target.id), None, **write_args(env, "k6"))

    order = c.get_customer_order("cus_9001", "SS90001")
    assert order.returns == () and order.items[0].status == OrderItemStatus.ACTIVE


def test_return_is_idempotent(env):
    c = env.commerce
    item = maya_item(env)
    first = c.create_return("cus_9001", "SS90001", item, "too_small", Decimal("49.99"), **write_args(env, "dup-1"))
    again = c.create_return("cus_9001", "SS90001", item, "too_small", Decimal("49.99"), **write_args(env, "dup-1"))
    assert again == first
    assert len(c.get_customer_order("cus_9001", "SS90001").returns) == 1

    with pytest.raises(DataAccessError):  # same key, different request
        c.create_return("cus_9001", "SS90001", item, "changed_mind", Decimal("49.99"), **write_args(env, "dup-1"))
    with pytest.raises(DataAccessError):  # new key, item already in a return
        c.create_return("cus_9001", "SS90001", item, "too_small", Decimal("49.99"), **write_args(env, "dup-2"))


def test_exchange_is_idempotent_and_reserves_stock_once(env):
    c = env.commerce
    item = maya_item(env)
    current = c.get_customer_order("cus_9001", "SS90001").items[0]
    target = next(v for v in c.get_product(current.product_id).variants
                  if v.stock_qty > 0 and v.id != current.variant_id)
    first = c.create_exchange("cus_9001", "SS90001", item, target.id, "too_small", **write_args(env, "ex-1"))
    again = c.create_exchange("cus_9001", "SS90001", item, target.id, "too_small", **write_args(env, "ex-1"))
    assert again == first
    after = next(v for v in c.get_product(current.product_id).variants if v.id == target.id)
    assert after.stock_qty == target.stock_qty - current.quantity
    assert c.count_item_exchanges("cus_9001", item) == 1


def test_cancel_is_idempotent(env):
    c = env.commerce
    first = c.cancel_order("cus_9002", "SS90005", idempotency_key="cx-1", now=NOW)
    again = c.cancel_order("cus_9002", "SS90005", idempotency_key="cx-1", now=NOW + timedelta(minutes=1))
    assert again == first and first.order_id == "SS90005"
    assert c.get_customer_order("cus_9002", "SS90005").status == OrderStatus.CANCELLED
