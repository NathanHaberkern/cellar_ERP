"""Shipping items for the ERP-wide daily to-do list.

Not wired into the Daily page yet (Nate: "eventually"). When it is, the hook
is one line in cellar/services/daily_plan.draft_items():

    items += shipping.services.todo.daily_items(today)

The dict shape matches daily_plan._auto_item, and every item carries a stable
auto_key, so re-drafting a day appends only what's new and never clobbers a
checked-off item.
"""
from __future__ import annotations

import uuid
from datetime import date

from django.utils import timezone

from shipping.models import PackPool, ShipOrder
from shipping.services.intake import compliance_gate_on, hold_reasons

S = ShipOrder.Status
UNSHIPPED = (S.NEW, S.ADDRESS_REVIEW, S.READY, S.ON_HOLD, S.POOLED)


def _item(label, detail, auto_key):
    return {"id": uuid.uuid4().hex[:12], "category": "Shipping", "label": label,
            "detail": detail, "lot_id": None, "lot_code": None, "done": False,
            "source": "auto", "auto_key": auto_key}


def daily_items(today: date | None = None) -> list[dict]:
    today = today or timezone.localdate()
    items = []

    # Requested ship dates that have arrived (or passed) on unshipped orders.
    due = (ShipOrder.objects.filter(status__in=UNSHIPPED, requested_ship_date__lte=today)
           .order_by("requested_ship_date", "c7_order_number"))
    for o in due:
        when = o.requested_ship_date
        late = " (late)" if when < today else ""
        items.append(_item(f"Ship #{o.c7_order_number}{late}",
                           f"{o.ship_name}, {o.ship_state} — requested {when:%b} {when.day}",
                           f"ship-requested-{o.pk}-{when.isoformat()}"))

    n_review = ShipOrder.objects.filter(status=S.ADDRESS_REVIEW).count()
    if n_review:
        items.append(_item(f"Review {n_review} shipping address{'es' if n_review > 1 else ''}",
                           "Shipping → Address review", f"ship-address-review-{today}"))

    unknown = [o for o in ShipOrder.objects.filter(status=S.READY).prefetch_related("lines")
               if any(ln.ml is None for ln in o.lines.all())]
    if unknown:
        items.append(_item("Set bottle sizes for unknown SKUs",
                           ", ".join(f"#{o.c7_order_number}" for o in unknown[:8]),
                           f"ship-unknown-ml-{today}"))

    if compliance_gate_on():
        held = [o for o in ShipOrder.objects.filter(status=S.READY).prefetch_related("lines")
                if any(r.startswith("Compliance") for r in hold_reasons(o, today))]
        if held:
            items.append(_item(f"Decide {len(held)} compliance hold{'s' if len(held) > 1 else ''}",
                               ", ".join(f"#{o.c7_order_number}" for o in held[:8]),
                               f"ship-compliance-{today}"))

    for pool in PackPool.objects.filter(status=PackPool.Status.OPEN):
        items.append(_item(f"Pack and label {pool.name}",
                           f"{pool.packages.count()} packages", f"ship-pool-{pool.pk}"))
    return items
