"""Build and dissolve pack pools in the database.

build_pools() takes every READY order with no hold reason, runs it through
the pure rules in services/packing.py, and writes PackPool + Package rows.
Pools start OPEN; an open pool with no labels can be dissolved, which puts its
orders back to READY so the run can be rebuilt (e.g. after a late order or a
box-size change). Once any label exists in a pool it can't be dissolved --
void the labels first.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shipping.models import BottleWeight, BoxSize, Package, PackPool, ShipOrder
from shipping.services.intake import hold_reasons, to_order_data
from shipping.services.packing import Box, build_shipments, plan_pools

S = ShipOrder.Status

# Used only if a bottle size has no BottleWeight row: 750 ml wine at 3.0 lb is
# the common case, scaled by volume otherwise. Nate's table in admin wins.
_FALLBACK_LB_PER_ML = Decimal("3.0") / Decimal("750")


def db_boxes() -> list[Box]:
    return [Box(name=b.name, use=b.use, max_bottles=b.max_bottles, tare_lb=b.tare_lb,
                max_weight_lb=b.max_weight_lb, length_in=b.length_in, width_in=b.width_in,
                height_in=b.height_in, pk=b.pk)
            for b in BoxSize.objects.filter(active=True)]


def weight_lookup():
    table = {w.ml: w.weight_lb for w in BottleWeight.objects.all()}

    def weight_for_ml(ml):
        if ml in table:
            return table[ml]
        return (Decimal(ml) * _FALLBACK_LB_PER_ML).quantize(Decimal("0.01"))
    return weight_for_ml


def eligible_orders(today: date | None = None):
    """READY orders with no computed hold reason, plus the held ones with reasons."""
    ready, held = [], []
    qs = (ShipOrder.objects.filter(status=S.READY).prefetch_related("lines")
          .order_by("ordered_at", "pk"))
    for order in qs:
        reasons = hold_reasons(order, today)
        (held if reasons else ready).append((order, reasons))
    return [o for o, _ in ready], held


def preview(today: date | None = None):
    """The pools build_pools() would make right now, without writing anything."""
    orders, _ = eligible_orders(today)
    conf = settings.SHIPPING
    shipments, problems = build_shipments(
        [to_order_data(o) for o in orders], db_boxes(), weight_lookup(),
        conf["FEDEX_RESTRICTED_STATES"])
    day = today or timezone.localdate()
    # f"{d:%b} {d.day}" rather than %-d: Windows strftime has no %-d.
    pools = plan_pools(shipments, default_skus=conf["DEFAULT_CLUB_SKUS"],
                       target=conf["POOL_TARGET_PACKAGES"], max_size=conf["POOL_MAX_PACKAGES"],
                       label_prefix=f"{day:%b} {day.day} ")
    return pools, problems, {str(o.pk): o for o in orders}


@transaction.atomic
def build_pools(user=None, today: date | None = None):
    """Write the previewed pools. Returns (PackPool list, problems)."""
    planned, problems, by_key = preview(today)
    box_by_pk = {b.pk: b for b in BoxSize.objects.all()}
    made = []
    for plan in planned:
        pool = PackPool.objects.create(name=plan.name[:60], carrier=plan.carrier,
                                       is_default=plan.is_default, created_by=user)
        seq = 0
        for shipment in plan.shipments:
            orders = [by_key[o.key] for o in shipment.orders]
            for pkg in shipment.packages:
                seq += 1
                package = Package.objects.create(
                    pool=pool, box=box_by_pk[pkg.box.pk], bottle_count=pkg.bottle_count,
                    weight_lb=pkg.weight_lb, contents=pkg.contents, sequence=seq)
                package.orders.set(orders)
            for order in orders:
                order.status = S.POOLED
                order.carrier = shipment.carrier
                order.carrier_reason = shipment.carrier_reason[:120]
                order.save(update_fields=["status", "carrier", "carrier_reason", "synced_at"])
        made.append(pool)
    return made, problems


class PoolLocked(Exception):
    pass


@transaction.atomic
def dissolve_pool(pool: PackPool):
    packages = list(pool.packages.all())
    if any(p.labels.exists() for p in packages):
        raise PoolLocked(f"{pool.name} has labels — void them before dissolving the pool")
    order_ids = set()
    for p in packages:
        order_ids |= set(p.orders.values_list("pk", flat=True))
        p.orders.clear()
        p.delete()
    ShipOrder.objects.filter(pk__in=order_ids, status=S.POOLED).update(
        status=S.READY, carrier="", carrier_reason="")
    pool.delete()
    return len(order_ids)
