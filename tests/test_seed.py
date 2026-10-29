import json
from decimal import Decimal

from sqlalchemy import func, select

from app.db.base import utcnow
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Product,
    ProductVariant,
    RefundStatus,
    ReturnItem,
    ReturnRequest,
    ReturnType,
)
from app.db.seed import Seeder


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_volumes_follow_settings(seeded_db, settings):
    scenarios = json.loads((settings.seed_data_dir / "scenarios.json").read_text())
    products = json.loads((settings.seed_data_dir / "products.json").read_text())

    assert count(seeded_db, Product) == len(products)
    assert count(seeded_db, Customer) == settings.seed_num_customers + len(scenarios["customers"])
    assert count(seeded_db, Order) == settings.seed_num_orders + len(scenarios["orders"])


def test_seed_is_deterministic(db, settings):
    now = utcnow()
    first = Seeder(db, settings, now=now).run()
    db.rollback()
    second = Seeder(db, settings, now=now).run()
    assert first == second


def test_order_totals_add_up(seeded_db):
    for order in seeded_db.scalars(select(Order)):
        items_total = sum(i.unit_price * i.quantity for i in order.items)
        assert order.subtotal == items_total
        assert order.total == order.subtotal - order.discount_total + order.shipping_fee


def test_order_timeline_is_consistent(seeded_db):
    now = utcnow()
    for o in seeded_db.scalars(select(Order)):
        if o.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
            assert o.tracking_number and o.carrier
            assert o.placed_at <= o.shipped_at
        if o.status == OrderStatus.DELIVERED:
            assert o.shipped_at <= o.delivered_at <= now
        if o.status in (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.FULFILLING):
            assert o.shipped_at is None and o.tracking_number is None


def test_guests_have_no_identity_and_members_do(seeded_db):
    for c in seeded_db.scalars(select(Customer)):
        assert (c.identity_subject is None) == c.is_guest


def test_final_sale_items_are_never_returned(seeded_db):
    returned_final_sale = seeded_db.scalar(
        select(func.count())
        .select_from(ReturnItem)
        .join(OrderItem, OrderItem.id == ReturnItem.order_item_id)
        .where(OrderItem.is_final_sale.is_(True))
    )
    assert returned_final_sale == 0


def test_returns_are_internally_consistent(seeded_db):
    for r in seeded_db.scalars(select(ReturnRequest)):
        item = seeded_db.get(OrderItem, r.items[0].order_item_id)
        assert item.order_id == r.order_id
        assert r.customer_id == r.order.customer_id
        if r.type == ReturnType.EXCHANGE:
            assert r.refund_status == RefundStatus.NOT_APPLICABLE
            assert r.refund_amount is None
            new = seeded_db.get(ProductVariant, r.items[0].exchange_variant_id)
            assert new.product_id == item.variant.product_id and new.id != item.variant_id
        else:
            assert r.refund_amount is not None and r.refund_amount > 0


def test_scenario_orders(seeded_db, settings):
    def order(oid):
        return seeded_db.get(Order, oid)

    now = utcnow()
    window = settings.return_window_days

    assert (now - order("SS90001").delivered_at).days < window
    assert (now - order("SS90002").delivered_at).days > window
    assert {i.is_final_sale for i in order("SS90003").items} == {True, False}
    assert order("SS90003").discount_code == "WELCOME10"
    assert order("SS90004").status == OrderStatus.SHIPPED
    assert order("SS90005").status == OrderStatus.PAID
    assert order("SS90006").status == OrderStatus.FULFILLING
    assert order("SS90007").items[0].status == OrderItemStatus.EXCHANGE_REQUESTED
    assert order("SS90008").returns[0].refund_status == RefundStatus.REFUNDED
    assert order("SS90009").returns[0].refund_status == RefundStatus.PROCESSING
    assert order("SS90010").customer.is_guest
    assert order("SS90011").status == OrderStatus.CANCELLED


def test_some_variants_out_of_stock_with_restock_dates(seeded_db):
    oos = seeded_db.scalars(select(ProductVariant).where(ProductVariant.stock_qty == 0)).all()
    assert oos
    assert any(v.restock_date for v in oos)
    assert all(v.size_us % Decimal("0.5") == 0 for v in oos)
