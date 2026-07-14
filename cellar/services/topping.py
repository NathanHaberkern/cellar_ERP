"""
Topping, partial-barrel fills, rack-out, and barrel-down.

This closes the loop the task rules opened: `rule_topping_interval` has been
raising "Top barrels — 25VERD" tasks since slice B, and there was nothing in the
system that could actually record a topping. Completing the task wrote a
TaskEvent and no wine moved.

The models already carry the hard parts. `ToppingTarget.save()` books the
evaporative VolumeLoss on the barrel's lot and, when the wine came from a
different lot, writes the LotLineage TOPPING edge that (a) feeds composition and
(b) is what debits the source. See services/volumes.py for why the source debit
is a lineage edge and not a second VolumeLoss.

`AgingPlacement.is_flagged` already implements the 5-gallon foreign-wine rule —
cumulative per barrel, cleared when the placement is emptied. Rack-out is what
empties it, so `rack_out()` is what clears the flag.

Two kinds of topping:
  ROUTINE      — replacing evaporation in a full barrel. Volume added == loss.
  PARTIAL_FILL — bringing a short barrel up to full. No loss: that wine was never
                 there to evaporate. The barrel stops being flagged as partial.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (
    AgingPlacement, Lot, ToppingEvent, ToppingTarget, VolumeLoss,
    VolumeMeasurement,
)
from cellar.services import tasks as task_svc
from cellar.services import volumes as vol_svc

ZERO = Decimal("0")
GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v is not None else ZERO


def _as_dt(d):
    """VolumeMeasurement.measured_at is a DateTimeField and the cellar works in
    dates. Coerce, in the current timezone, rather than handing Django a naive
    datetime and letting it guess."""
    from datetime import date as _date, datetime, time
    if isinstance(d, datetime):
        return d if timezone.is_aware(d) else timezone.make_aware(d)
    if isinstance(d, _date):
        return timezone.make_aware(datetime.combine(d, time(12, 0)))
    return d


class InsufficientWine(ValueError):
    """The source lot doesn't hold enough wine to do this."""


# ======================================================================
# Topping
# ======================================================================
@transaction.atomic
def top_barrels(source_lot, *, topped_at, placements=None, total_gal=None,
                per_barrel=None, kind=ToppingEvent.Kind.ROUTINE, actor=None,
                allow_overdraw=False):
    """Top one or more barrels from `source_lot`.

    placements : AgingPlacement rows (or pks) being topped.
    total_gal  : total drawn from the source; split evenly across the barrels.
    per_barrel : {placement_pk: gallons} — overrides the even split. Either this
                 or total_gal is required; per_barrel wins if both are given.

    Returns the ToppingEvent.
    """
    if not placements:
        raise ValueError("Select at least one barrel to top.")

    rows = []
    for p in placements:
        if not isinstance(p, AgingPlacement):
            p = AgingPlacement.objects.select_related("container", "lot").get(pk=p)
        if p.emptied_at is not None:
            raise ValueError(f"{p.container.container_id} has already been racked out.")
        rows.append(p)

    # ---- allocate gallons across the barrels
    if per_barrel:
        alloc = {p.pk: _d(per_barrel[p.pk]) for p in rows if per_barrel.get(p.pk)}
        if not alloc:
            raise ValueError("No gallons allocated to any barrel.")
    else:
        if total_gal in (None, "", 0):
            raise ValueError("Enter the total gallons drawn from the source lot.")
        total = _d(total_gal)
        each = (total / len(rows)).quantize(GAL)
        alloc = {p.pk: each for p in rows}
        # push the rounding remainder onto the last barrel so the draw ties out
        drift = total - (each * len(rows))
        if drift:
            alloc[rows[-1].pk] = (alloc[rows[-1].pk] + drift).quantize(GAL)

    drawn = sum(alloc.values(), ZERO)

    # ---- the source must be able to cover it
    balance = vol_svc.lot_balance(source_lot)
    if balance is not None and drawn > balance and not allow_overdraw:
        raise InsufficientWine(
            f"{source_lot.code} holds {balance} gal; you're drawing {drawn} gal. "
            f"Check the source, or gauge the lot before topping.")

    # ---- a partial fill can't overfill the barrel
    if kind == ToppingEvent.Kind.PARTIAL_FILL:
        for p in rows:
            room = vol_svc.ullage(p)
            if alloc[p.pk] > room + Decimal("0.5"):
                raise ValueError(
                    f"{p.container.container_id} only has {room} gal of ullage; "
                    f"you're adding {alloc[p.pk]} gal.")

    event = ToppingEvent.objects.create(
        source_lot=source_lot, kind=kind, topped_at=topped_at)

    for p in rows:
        # ToppingTarget.save() books the evaporative VolumeLoss (routine only)
        # and the foreign-contribution lineage edge when source != barrel lot.
        ToppingTarget.objects.create(
            event=event, placement=p, volume_added=alloc[p.pk])

    # close out any open "top this barrel" tasks for the barrels we just topped
    _close_topping_tasks(rows, actor=actor, detail=f"topped from {source_lot.code}")
    return event


def _close_topping_tasks(placements, actor=None, detail=""):
    from cellar.models import Task
    containers = [p.container_id for p in placements]
    lots = {p.lot_id for p in placements}
    open_tasks = Task.objects.filter(
        status=Task.Status.OPEN,
    ).filter(container_id__in=containers) | Task.objects.filter(
        status=Task.Status.OPEN, lot_id__in=lots,
        dedupe_key__startswith="toptop:")
    for t in open_tasks.distinct():
        task_svc.complete_task(t, actor=actor, detail=detail)


def top_partial(source_lot, placement, *, topped_at, gallons=None, actor=None):
    """Fill a partial barrel up to full (or by `gallons`) from another lot."""
    gallons = _d(gallons) if gallons else vol_svc.ullage(placement)
    if gallons <= 0:
        raise ValueError(f"{placement.container.container_id} is already full.")
    return top_barrels(source_lot, topped_at=topped_at, placements=[placement],
                       total_gal=gallons, kind=ToppingEvent.Kind.PARTIAL_FILL,
                       actor=actor)


# ======================================================================
# Rack-out / barrel-down
# ======================================================================
@transaction.atomic
def rack_out(placements, *, racked_at, to_vessel=None, gauged_gal=None,
             loss_reason="rack-out loss", actor=None):
    """Empty barrels back to a vessel.

    This is the deliberate rack-out that clears the 5-gallon foreign-wine flag
    (`AgingPlacement.is_flagged` reads `emptied_at`), and it is where a long-aged
    port's accrued evaporation gets trued up: if you gauge the wine coming out,
    the difference between what the books say and what came out is booked as a
    real loss (or backed out, if we over-accrued).

    gauged_gal : what actually came out. Blank → no true-up, books stand.
    """
    rows = []
    for p in placements:
        if not isinstance(p, AgingPlacement):
            p = AgingPlacement.objects.select_related("container", "lot").get(pk=p)
        if p.emptied_at is not None:
            raise ValueError(f"{p.container.container_id} is already empty.")
        rows.append(p)

    lots = {p.lot_id for p in rows}
    if len(lots) > 1:
        raise ValueError("Rack out one lot at a time — these barrels hold different lots.")
    lot = rows[0].lot

    on_books = sum((vol_svc.placement_volume(p) for p in rows), ZERO)

    for p in rows:
        p.emptied_at = racked_at
        p.save(update_fields=["emptied_at"])

    trued_up = None
    if gauged_gal not in (None, ""):
        gauged = _d(gauged_gal)
        delta = (on_books - gauged).quantize(GAL)
        if delta > 0:
            trued_up = VolumeLoss.objects.create(
                lot=lot, volume_gal=delta, reason=loss_reason, occurred_at=racked_at)
        elif delta < 0:
            # we over-accrued evaporation; give it back rather than leave the
            # books below the wine that actually exists
            trued_up = VolumeLoss.objects.create(
                lot=lot, volume_gal=delta, reason="accrual true-up (over-accrued)",
                occurred_at=racked_at)
        VolumeMeasurement.objects.create(
            lot=lot, method=VolumeMeasurement.Method.BARREL_BACKFILL,
            measured_at=_as_dt(racked_at), volume_gal=gauged, barrels_filled=len(rows))

    if to_vessel is not None:
        from cellar.services import operations as ops
        ops.transfer_lot(lot, to_vessel, racked_at)

    return {"lot": lot, "barrels": len(rows), "on_books": on_books,
            "gauged": _d(gauged_gal) if gauged_gal else None,
            "true_up": trued_up}
