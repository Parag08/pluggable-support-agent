"""Deterministic seed data for local development, tests, and evals.

Volumes and rates come from settings (.env); catalog, stores, size chart,
names, carriers, and fixed scenario orders come from files in SEED_DATA_DIR.
Dates are relative to seed time so return-window logic stays meaningful.
"""

import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    ActorType,
    Customer,
    DiscountCode,
    Gender,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Product,
    ProductVariant,
    RefundStatus,
    ReturnItem,
    ReturnReason,
    ReturnRequest,
    ReturnStatus,
    ReturnType,
    SizeChart,
    Store,
    Width,
)

CENT = Decimal("0.01")


def money(value: Decimal | str | float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def size_steps(lo: float, hi: float, step: float) -> list[Decimal]:
    sizes, x = [], Decimal(str(lo))
    while x <= Decimal(str(hi)):
        sizes.append(x)
        x += Decimal(str(step))
    return sizes


@dataclass
class SeedSummary:
    products: int = 0
    variants: int = 0
    size_chart_rows: int = 0
    stores: int = 0
    discount_codes: int = 0
    customers: int = 0
    orders: int = 0
    order_items: int = 0
    returns: int = 0
    scenario_orders: int = 0


class Seeder:
    def __init__(self, db: Session, settings: Settings, now: datetime | None = None):
        self.db = db
        self.s = settings
        self.now = now or utcnow()
        self.rng = random.Random(settings.seed_random_seed)
        self.ref = self._load("reference.json")
        self.summary = SeedSummary()

        self.products: dict[str, Product] = {}
        self.variants: dict[tuple[str, str, Decimal, str], ProductVariant] = {}
        self.discounts: dict[str, DiscountCode] = {}
        self._return_seq = self.ref["return_id_start"]

    # --- helpers ------------------------------------------------------------

    def _load(self, name: str) -> Any:
        path: Path = self.s.seed_data_dir / name
        return json.loads(path.read_text(encoding="utf-8"))

    def _weighted(self, weights: dict[str, int]) -> str:
        keys = list(weights)
        return self.rng.choices(keys, weights=[weights[k] for k in keys])[0]

    def _days_ago(self, days: float) -> datetime:
        return self.now - timedelta(days=days)

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    # --- catalog ------------------------------------------------------------

    def seed_catalog(self) -> None:
        ranges = self.ref["size_ranges_us"]
        stock_lo, stock_hi = self.ref["stock_qty_range"]
        restock_lo, restock_hi = self.ref["restock_days_range"]

        for p in self._load("products.json"):
            product = Product(
                id=p["id"],
                handle=p["handle"],
                sku_prefix=p["sku_prefix"],
                name=p["name"],
                category=p["category"],
                gender=Gender(p["gender"]),
                description=p["description"],
                materials=p["materials"],
                features=p["features"],
                care_instructions=p["care_instructions"],
                fit_note=p["fit_note"],
                fit_note_detail=p["fit_note_detail"],
                fit_size_offset=Decimal(str(p["fit_size_offset"])),
                price=money(p["price"]),
                currency=self.s.store_currency,
                is_final_sale=p["is_final_sale"],
                is_active=True,
                created_at=self._days_ago(self.ref["product_created_days_ago"]),
            )
            self.db.add(product)
            self.products[product.handle] = product
            self.summary.products += 1

            r = ranges[p["gender"]]
            for color, color_code in p["colors"].items():
                for width in p["widths"]:
                    for size in size_steps(r["min"], r["max"], r["step"]):
                        out_of_stock = self.rng.random() < self.s.seed_out_of_stock_rate
                        restock = None
                        if out_of_stock and self.rng.random() < self.s.seed_restock_probability:
                            restock = (self.now + timedelta(days=self.rng.randint(restock_lo, restock_hi))).date()
                        variant = ProductVariant(
                            product=product,
                            sku=f"{p['sku_prefix']}-{color_code}-{width[0].upper()}-{int(size * 10):03d}",
                            color=color,
                            size_us=size,
                            width=Width(width),
                            stock_qty=0 if out_of_stock else self.rng.randint(stock_lo, stock_hi),
                            restock_date=restock,
                        )
                        self.db.add(variant)
                        self.variants[(product.handle, color, size, width)] = variant
                        self.summary.variants += 1

        for row in self._load("size_chart.json")["rows"]:
            self.db.add(SizeChart(
                gender=Gender(row["gender"]),
                us=Decimal(str(row["us"])),
                uk=Decimal(str(row["uk"])),
                eu=Decimal(str(row["eu"])),
                foot_length_cm=Decimal(str(row["foot_length_cm"])),
            ))
            self.summary.size_chart_rows += 1

        for st in self._load("stores.json"):
            self.db.add(Store(**{**st, "latitude": Decimal(st["latitude"]), "longitude": Decimal(st["longitude"])}))
            self.summary.stores += 1

        for d in self._load("discount_codes.json"):
            code = DiscountCode(
                code=d["code"],
                description=d["description"],
                percent_off=Decimal(d["percent_off"]) if d["percent_off"] else None,
                amount_off=money(d["amount_off"]) if d["amount_off"] else None,
                min_subtotal=money(d["min_subtotal"]) if d["min_subtotal"] else None,
                starts_at=self._days_ago(d["starts_at_days_ago"]),
                ends_at=self.now + timedelta(days=d["ends_in_days"]),
                is_active=d["is_active"],
            )
            self.db.add(code)
            self.discounts[code.code] = code
            self.summary.discount_codes += 1

        self.db.flush()

    # --- customers ----------------------------------------------------------

    def _address(self, first: str, last: str) -> dict:
        people = self.people
        city = self.rng.choice(people["cities"])
        return {
            "name": f"{first} {last}",
            "line1": f"{self.rng.randint(*self.ref['street_number_range'])} {self.rng.choice(people['streets'])}",
            "line2": None,
            "city": city["city"],
            "region": city["region"],
            "postal_code": f"{city['postal_prefix']}{self.rng.randint(0, 99):02d}",
            "country": people["country"],
        }

    def seed_customers(self) -> list[Customer]:
        self.people = self._load("people.json")
        n = self.s.seed_num_customers
        n_guests = round(n * self.s.seed_guest_customer_ratio)
        guest_idx = set(self.rng.sample(range(n), n_guests))
        seen_emails: set[str] = set()
        customers = []

        for i in range(n):
            first = self.rng.choice(self.people["first_names"])
            last = self.rng.choice(self.people["last_names"])
            local = f"{first}.{last}".lower()
            email, k = f"{local}@{self.ref['email_domain']}", 1
            while email in seen_emails:
                k += 1
                email = f"{local}{k}@{self.ref['email_domain']}"
            seen_emails.add(email)

            is_guest = i in guest_idx
            customer = Customer(
                id=f"{self.ref['customer_id_prefix']}{i + 1:04d}",
                identity_subject=None if is_guest else f"{self.ref['identity_subject_prefix']}{self._uuid()}",
                email=email,
                first_name=first,
                last_name=last,
                phone=self.people["phone_template"].format(nn=i % 100) if self.rng.random() < self.ref["customer_phone_rate"] else None,
                is_guest=is_guest,
                default_shipping_address=self._address(first, last),
                created_at=self._days_ago(
                    self.s.seed_order_history_days
                    + self.rng.randint(*self.ref["customer_created_days_before_history_range"])
                ),
            )
            self.db.add(customer)
            customers.append(customer)
        self.summary.customers += n
        self.db.flush()
        return customers

    # --- orders -------------------------------------------------------------

    def _apply_discount(self, code: str | None, subtotal: Decimal) -> tuple[str | None, Decimal]:
        if not code:
            return None, money(0)
        d = self.discounts[code]
        if d.min_subtotal and subtotal < d.min_subtotal:
            return None, money(0)
        if d.percent_off:
            return code, money(subtotal * d.percent_off / 100)
        return code, min(money(d.amount_off), subtotal)

    def _tracking(self) -> tuple[str, str, str]:
        carrier = self.rng.choice(self.ref["carriers"])
        digits = "".join(str(self.rng.randint(0, 9)) for _ in range(carrier["tracking_digits"]))
        number = f"{carrier['tracking_prefix']}{digits}"
        url = self.ref["tracking_url_template"].format(carrier=carrier["name"].lower(), tracking_number=number)
        return carrier["name"], number, url

    def _build_order(
        self,
        order_id: str,
        customer: Customer,
        status: OrderStatus,
        placed_at: datetime,
        lines: list[tuple[ProductVariant, int]],
        discount_code: str | None,
        shipped_at: datetime | None = None,
        delivered_at: datetime | None = None,
    ) -> Order:
        subtotal = money(sum(v.product.price * q for v, q in lines))
        code, discount = self._apply_discount(discount_code, subtotal)
        shipping = money(0) if subtotal >= self.s.free_shipping_threshold else money(self.s.standard_shipping_fee)

        order = Order(
            id=order_id,
            customer=customer,
            status=status,
            currency=self.s.store_currency,
            subtotal=subtotal,
            discount_total=discount,
            shipping_fee=shipping,
            total=money(subtotal - discount + shipping),
            discount_code=code,
            shipping_address=customer.default_shipping_address,
            placed_at=placed_at,
        )

        if status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED):
            f_lo, f_hi = self.ref["fulfilment_hours_range"]
            t_lo, t_hi = self.ref["transit_days_range"]
            order.fulfilled_at = placed_at + timedelta(hours=self.rng.randint(f_lo, f_hi))
            order.shipped_at = shipped_at or order.fulfilled_at
            order.carrier, order.tracking_number, order.tracking_url = self._tracking()
            order.estimated_delivery_date = (order.shipped_at + timedelta(days=self.rng.randint(t_lo, t_hi))).date()
            if status == OrderStatus.DELIVERED:
                order.delivered_at = delivered_at or (order.shipped_at + timedelta(days=self.rng.randint(t_lo, t_hi)))
        elif status == OrderStatus.CANCELLED:
            order.cancelled_at = placed_at + timedelta(hours=self.rng.randint(*self.ref["cancellation_hours_after_order_range"]))

        item_status = OrderItemStatus.CANCELLED if status == OrderStatus.CANCELLED else OrderItemStatus.ACTIVE
        for variant, qty in lines:
            self.db.add(OrderItem(
                order=order,
                variant=variant,
                product_name=variant.product.name,
                sku=variant.sku,
                color=variant.color,
                size_us=variant.size_us,
                width=variant.width,
                quantity=qty,
                unit_price=variant.product.price,
                is_final_sale=variant.product.is_final_sale,
                status=item_status,
            ))
            self.summary.order_items += 1

        self.db.add(order)
        self.summary.orders += 1
        return order

    def _random_status(self, placed_at: datetime) -> tuple[OrderStatus, datetime | None, datetime | None]:
        if self.rng.random() < self.s.seed_cancellation_rate:
            return OrderStatus.CANCELLED, None, None
        age_h = (self.now - placed_at).total_seconds() / 3600
        f_lo, f_hi = self.ref["fulfilment_hours_range"]
        t_lo, t_hi = self.ref["transit_days_range"]
        if age_h < 1:
            return OrderStatus.PENDING, None, None
        if age_h < f_lo:
            return OrderStatus.PAID, None, None
        if age_h < f_hi:
            return OrderStatus.FULFILLING, None, None
        shipped = placed_at + timedelta(hours=self.rng.randint(f_lo, f_hi))
        delivered = shipped + timedelta(days=self.rng.randint(t_lo, t_hi))
        if delivered > self.now:
            return OrderStatus.SHIPPED, shipped, None
        return OrderStatus.DELIVERED, shipped, delivered

    def seed_random_orders(self, customers: list[Customer]) -> None:
        handles = list(self.products)
        variants_by_handle: dict[str, list[ProductVariant]] = {}
        for (handle, *_), v in self.variants.items():
            variants_by_handle.setdefault(handle, []).append(v)
        active_codes = [c for c, d in self.discounts.items() if d.is_active]

        placed = sorted(
            self._days_ago(self.rng.uniform(0, self.s.seed_order_history_days))
            for _ in range(self.s.seed_num_orders)
        )
        for i, placed_at in enumerate(placed):
            customer = self.rng.choice(customers)
            n_items = int(self._weighted(self.ref["items_per_order_weights"]))
            lines = [
                (self.rng.choice(variants_by_handle[self.rng.choice(handles)]),
                 int(self._weighted(self.ref["quantity_weights"])))
                for _ in range(n_items)
            ]
            code = self.rng.choice(active_codes) if self.rng.random() < self.s.seed_discount_usage_rate else None
            status, shipped, delivered = self._random_status(placed_at)
            order = self._build_order(
                f"{self.ref['order_id_prefix']}{self.ref['order_id_start'] + i}",
                customer, status, placed_at, lines, code, shipped, delivered,
            )
            if status == OrderStatus.DELIVERED and self.rng.random() < self.s.seed_return_rate:
                self._random_return(order)
        self.db.flush()

    # --- returns ------------------------------------------------------------

    def _next_return_id(self) -> str:
        rid = f"{self.ref['return_id_prefix']}{self._return_seq}"
        self._return_seq += 1
        return rid

    def _exchange_variant(self, item: OrderItem, reason: ReturnReason) -> ProductVariant | None:
        handle = item.variant.product.handle
        size, width = item.size_us, item.width.value
        if reason == ReturnReason.TOO_SMALL:
            size += Decimal("0.5")
        elif reason == ReturnReason.TOO_LARGE:
            size -= Decimal("0.5")
        elif reason == ReturnReason.TOO_NARROW:
            width = Width.WIDE.value
        elif reason == ReturnReason.TOO_WIDE:
            width = Width.REGULAR.value if width == Width.WIDE.value else Width.NARROW.value
        else:
            return None
        return self.variants.get((handle, item.color, size, width))

    def _create_return(
        self,
        order: Order,
        item: OrderItem,
        rtype: ReturnType,
        status: ReturnStatus,
        reason: ReturnReason,
        refund_status: RefundStatus,
        created_at: datetime,
        refunded_at: datetime | None = None,
        exchange_variant: ProductVariant | None = None,
    ) -> ReturnRequest:
        rid = self._next_return_id()
        discount_ratio = (order.discount_total / order.subtotal) if order.subtotal else Decimal(0)
        refund_amount = None
        if rtype == ReturnType.RETURN:
            refund_amount = money(item.unit_price * item.quantity * (1 - discount_ratio))

        ret = ReturnRequest(
            id=rid,
            order=order,
            customer_id=order.customer.id,
            type=rtype,
            status=status,
            reason=reason,
            refund_amount=refund_amount,
            refund_status=refund_status,
            refunded_at=refunded_at,
            label_url=self.ref["return_label_url_template"].format(return_id=rid)
            if status != ReturnStatus.REQUESTED else None,
            created_by=self.rng.choice([ActorType.HUMAN, ActorType.CUSTOMER_PORTAL]),
            created_at=created_at,
            updated_at=refunded_at or created_at,
        )
        self.db.add(ret)
        self.db.add(ReturnItem(
            return_request=ret,
            order_item_id=item.id,
            quantity=item.quantity,
            exchange_variant_id=exchange_variant.id if exchange_variant else None,
        ))

        done = status == ReturnStatus.COMPLETED
        if rtype == ReturnType.EXCHANGE:
            item.status = OrderItemStatus.EXCHANGED if done else OrderItemStatus.EXCHANGE_REQUESTED
        elif status != ReturnStatus.REJECTED:
            item.status = OrderItemStatus.RETURNED if done else OrderItemStatus.RETURN_REQUESTED
        self.summary.returns += 1
        return ret

    def _random_return(self, order: Order) -> None:
        self.db.flush()  # need item ids
        eligible = [i for i in order.items if not i.is_final_sale]
        days_since = (self.now - order.delivered_at).total_seconds() / 86400
        if not eligible or days_since < 1:
            return
        item = self.rng.choice(eligible)
        created_at = order.delivered_at + timedelta(days=self.rng.uniform(0.5, min(self.s.return_window_days, days_since)))
        age = (self.now - created_at).days

        reason = ReturnReason(self._weighted(self.ref["return_reason_weights"]))
        exchange_variant = None
        rtype = ReturnType.RETURN
        if self.rng.random() < self.ref["exchange_share_of_returns"]:
            exchange_variant = self._exchange_variant(item, reason)
            if exchange_variant:
                rtype = ReturnType.EXCHANGE

        by_age = self.ref["return_status_by_age_days"]
        if age < by_age["in_transit_after"]:
            status = self.rng.choice([ReturnStatus.REQUESTED, ReturnStatus.LABEL_SENT])
        elif age < by_age["received_after"]:
            status = ReturnStatus.IN_TRANSIT
        elif age < by_age["completed_after"]:
            status = ReturnStatus.RECEIVED
        elif self.rng.random() < self.ref["return_rejection_rate"]:
            status = ReturnStatus.REJECTED
        else:
            status = ReturnStatus.COMPLETED

        refunded_at = None
        if rtype == ReturnType.EXCHANGE or status == ReturnStatus.REJECTED:
            refund_status = RefundStatus.NOT_APPLICABLE
        elif status == ReturnStatus.COMPLETED:
            refund_status = RefundStatus.REFUNDED
            refunded_at = created_at + timedelta(days=self.rng.randint(*self.ref["refund_days_after_return_range"]))
        elif status == ReturnStatus.RECEIVED:
            refund_status = RefundStatus.PROCESSING
        else:
            refund_status = RefundStatus.PENDING

        self._create_return(order, item, rtype, status, reason, refund_status, created_at, refunded_at, exchange_variant)

    # --- scenarios ----------------------------------------------------------

    def _variant(self, handle: str, color: str, size_us: float, width: str) -> ProductVariant:
        key = (handle, color, Decimal(str(size_us)), width)
        try:
            return self.variants[key]
        except KeyError:
            raise ValueError(f"scenarios.json references a variant that doesn't exist: {key}") from None

    def seed_scenarios(self) -> None:
        data = self._load("scenarios.json")
        if not hasattr(self, "people"):
            self.people = self._load("people.json")

        customers: dict[str, Customer] = {}
        for c in data["customers"]:
            customer = Customer(
                id=c["id"],
                identity_subject=c["identity_subject"],
                email=c["email"],
                first_name=c["first_name"],
                last_name=c["last_name"],
                phone=None,
                is_guest=c["is_guest"],
                default_shipping_address=self._address(c["first_name"], c["last_name"]),
                created_at=self._days_ago(self.s.seed_order_history_days + c["created_days_before_history"]),
            )
            self.db.add(customer)
            customers[customer.id] = customer
            self.summary.customers += 1

        for o in data["orders"]:
            lines = [(self._variant(i["handle"], i["color"], i["size_us"], i["width"]), i["quantity"]) for i in o["items"]]
            order = self._build_order(
                o["id"],
                customers[o["customer_id"]],
                OrderStatus(o["status"]),
                self._days_ago(o["placed_days_ago"]),
                lines,
                o.get("discount_code"),
                shipped_at=self._days_ago(o["shipped_days_ago"]) if "shipped_days_ago" in o else None,
                delivered_at=self._days_ago(o["delivered_days_ago"]) if "delivered_days_ago" in o else None,
            )
            self.summary.scenario_orders += 1

            if r := o.get("return"):
                self.db.flush()
                ex = r.get("exchange_to")
                item = order.items[0]
                self._create_return(
                    order,
                    item,
                    ReturnType(r["type"]),
                    ReturnStatus(r["status"]),
                    ReturnReason(r["reason"]),
                    RefundStatus(r["refund_status"]),
                    self._days_ago(r["created_days_ago"]),
                    self._days_ago(r["refunded_days_ago"]) if "refunded_days_ago" in r else None,
                    self._variant(item.variant.product.handle, ex["color"], ex["size_us"], ex["width"]) if ex else None,
                )
        self.db.flush()

    # --- entry point --------------------------------------------------------

    def run(self) -> SeedSummary:
        self.seed_catalog()
        customers = self.seed_customers()
        self.seed_random_orders(customers)
        self.seed_scenarios()
        return self.summary


def seed(db: Session, settings: Settings, now: datetime | None = None) -> SeedSummary:
    summary = Seeder(db, settings, now).run()
    db.commit()
    return summary
