"""
Angel's share accrual for long-aged port.

Ordinary aging needs no accrual: the barrel is topped, the topping records the
evaporation, and the loss lands on the books the day it is replaced.

Port is different. We hold port barrels in bond for 10–20 years without topping
them — by bottling, more than half the wine is gone. Nothing records that, because
nothing ever tops those barrels. The books carry the fill volume for two decades
and then discover, at barrel-down, that half the wine evaporated years ago. That
is a large unreported inventory loss showing up in one month, and it's not what
happened.

So: for port placements over 5 years old that have never been topped, accrue
4%/year (Nate's figure) as a VolumeLoss, once per calendar year. It's an estimate,
and it's labelled as one. `topping.rack_out(gauged_gal=...)` trues it up to the
actual barrel-down gauge — over-accrual is given back, under-accrual is booked.

Idempotent: the reason string carries the year, so re-running a year is a no-op.
Runs from the `accrue_evaporation` management command (call it at year end).
"""
from decimal import Decimal

from django.db import transaction

from cellar.models import AgingPlacement, ToppingTarget, VolumeLoss
from cellar.services import lotmeta
from cellar.services import volumes as vol_svc

ANNUAL_RATE = Decimal("0.04")     # 4%/yr — Nate, 2026-07
MIN_AGE_YEARS = 5
GAL = Decimal("0.1")

REASON = "angel's share accrual {year} (estimated, {rate}%/yr)"


def _reason(year):
    return REASON.format(year=year, rate=int(ANNUAL_RATE * 100))


def eligible_placements(year):
    """Port barrels, still full of wine, filled more than 5 years ago, never topped."""
    from datetime import date
    cutoff = date(year - MIN_AGE_YEARS, 12, 31)
    out = []
    qs = (AgingPlacement.objects
          .filter(emptied_at__isnull=True, voided_at__isnull=True,
                  filled_at__lte=cutoff)
          .select_related("container", "lot__current_designation"))
    for p in qs:
        if not getattr(p.container, "is_oak", False):
            continue
        if not lotmeta.is_port(p.lot):
            continue          # only port sits untopped for a decade
        if ToppingTarget.objects.filter(placement=p, voided_at__isnull=True).exists():
            continue          # it gets topped — the topping already books the loss
        out.append(p)
    return out


def plan(year):
    """What the accrual would book, per lot. Dry-run fodder."""
    from datetime import date
    by_lot = {}
    for p in eligible_placements(year):
        vol = vol_svc.placement_volume(p)
        loss = (vol * ANNUAL_RATE).quantize(GAL)
        if loss <= 0:
            continue
        row = by_lot.setdefault(p.lot_id, {"lot": p.lot, "barrels": 0,
                                           "on_books": Decimal("0"), "loss": Decimal("0")})
        row["barrels"] += 1
        row["on_books"] += vol
        row["loss"] += loss

    already = set(VolumeLoss.objects
                  .filter(reason=_reason(year), voided_at__isnull=True)
                  .values_list("lot_id", flat=True))
    for lot_id, row in by_lot.items():
        row["already_booked"] = lot_id in already
    return by_lot


@transaction.atomic
def accrue(year, commit=False):
    """Book one year of estimated evaporation on long-aged port."""
    from datetime import date
    booked_on = date(year, 12, 31)
    rows = plan(year)
    made = []
    for lot_id, row in rows.items():
        if row["already_booked"] or not commit:
            continue
        made.append(VolumeLoss.objects.create(
            lot=row["lot"], volume_gal=row["loss"].quantize(GAL),
            reason=_reason(year), occurred_at=booked_on))
    return {"plan": rows, "created": made, "committed": commit}
