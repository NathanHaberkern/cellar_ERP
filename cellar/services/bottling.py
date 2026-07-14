"""
Bottling — splitting a parcel off a finished bulk lot, and bottling it.

The problem this solves: reusing one code (25VERD) for the bulk wine in tank, the
parcel that was racked off and prepped, and the finished SKU means the history of
all three is smeared together — and the half left in tank for bulk sale becomes
untraceable the moment the other half is fined, blended, and bottled.

So: anything that goes to the filler is its OWN lot.

    weigh tags -> 25VERD  (bulk, 1200 gal)
                    |-- BOTTLING_SPLIT 600 gal -> 25VERD_B1 -> BottlingRun -> SKU
                    `-- 600 gal remains ----------------------------------> bulk sale

The parcel carries the prep work (fining, SO2, blending); the parent keeps its own
history and its remaining volume. `BottlingRun.sku` is the finished-goods identity
that matches Commerce7 / QBO — it is a SEPARATE field from the lot code, so the SKU
never has to carry the _B1 suffix and your C7 catalogue never changes.

Volume is conserved on the split: the parcel is credited, the parent debited, both
as new VolumeMeasurement rows (the ledger is append-only — we never edit the old
reading). 5120.17 then reads the bottling exactly as it should, because
BottlingRun already books A13 bulk->bottled, B2 bottled, and A29 bottling loss.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (Lot, LotLineage, BottlingRun, BottleFormat,
                           TankAssignment, VolumeMeasurement)
from cellar.models.base import LotKind
from cellar.services import generator
from cellar.services import operations as ops

# A parcel can only come off wine that's finished primary.
SPLITTABLE = {Lot.Status.DONE_PRIMARY}


def is_parcel(lot):
    d = lot.current_designation
    return bool(d and d.kind == LotKind.BOTTLING)


def parcels_of(lot):
    """Bottling parcels split off this lot, newest first."""
    edges = (LotLineage.objects
             .filter(parent_lot=lot, voided_at__isnull=True,
                     relationship_type=LotLineage.Relationship.BOTTLING_SPLIT)
             .select_related("child_lot__current_designation")
             .order_by("-created_at"))
    return [{"lot": e.child_lot, "volume_gal": e.volume_gal} for e in edges]


def parent_of(parcel):
    e = (LotLineage.objects
         .filter(child_lot=parcel, voided_at__isnull=True,
                 relationship_type=LotLineage.Relationship.BOTTLING_SPLIT)
         .select_related("parent_lot").first())
    return e.parent_lot if e else None


def can_split(lot):
    return lot.status in SPLITTABLE and not is_parcel(lot)


@transaction.atomic
def create_parcel(parent, *, volume_gal, vessel=None, at=None,
                  suffix=None, actor=None):
    """Rack `volume_gal` off `parent` into a new bottling parcel lot.

    Returns the child Lot. Raises if the volume exceeds what the parent has.
    """
    at = at or timezone.now()
    vol = Decimal(str(volume_gal))
    if vol <= 0:
        raise ValueError("Enter the gallons to rack off for bottling.")

    available = ops.current_volume(parent)
    if available is not None and vol > available:
        raise ValueError(
            f"{parent.code} holds {available:g} gal — can't rack off {vol:g} gal.")

    child = Lot.objects.create(vintage_year=parent.vintage_year,
                               status=Lot.Status.DONE_PRIMARY,
                               production_intent=parent.production_intent)
    generator.assign_parcel_designation(child, parent, suffix=suffix)

    LotLineage.objects.create(
        parent_lot=parent, child_lot=child,
        relationship_type=LotLineage.Relationship.BOTTLING_SPLIT,
        volume_gal=vol)

    # volume moves: credit the parcel, debit the parent (append-only, both)
    ops._record_volume(child, vol, at)
    if available is not None:
        ops._record_volume(parent, (available - vol).quantize(Decimal("0.1")), at)

    if vessel is not None:
        ops.assign_lot_to_vessel(child, vessel, at)

    return child


@transaction.atomic
def bottle_parcel(lot, *, sku, bottle_format, cases_produced, bottled_at=None,
                  bulk_gallons_in=None, line_labor_cost=0, actor=None):
    """Bottle a parcel: record the run, empty its vessel, mark the lot bottled.

    `sku` is the finished-goods identity (C7 / QBO) and is deliberately independent
    of the lot code — bottling 25VERD_B1 produces SKU "25VERD" if that's what your
    catalogue calls it.
    """
    bottled_at = bottled_at or timezone.localdate()
    if isinstance(bottle_format, (int, str)):
        bottle_format = BottleFormat.objects.get(pk=bottle_format)
    if not sku:
        raise ValueError("A finished-goods SKU is required (must match C7 / QBO).")

    run = BottlingRun.objects.create(
        source_lot=lot, bottle_format=bottle_format, sku=sku,
        bottled_at=bottled_at, cases_produced=int(cases_produced),
        bulk_gallons_in=(Decimal(str(bulk_gallons_in))
                         if bulk_gallons_in not in (None, "") else None),
        line_labor_cost=Decimal(str(line_labor_cost or 0)))

    # the wine has left the tank
    (TankAssignment.objects
     .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
     .update(emptied_at=timezone.now()))
    ops._record_volume(lot, Decimal("0"), timezone.now())

    lot.status = Lot.Status.BOTTLED
    lot.save(update_fields=["status"])

    _apply_removal_basis(run, actor=actor)
    return run


# ======================================================================
# Excise removal basis
# ======================================================================
REMOVAL_BASIS_KEY = "excise_removal_basis"


def removal_basis():
    """'bottling' — tax is paid when the wine is bottled; the whole run is removed
    taxpaid on the bottling date and Section B never carries a balance. This is what
    St. Amant does, and what every filed 2025 report reflects.

    'shipment' — the run stays in bond as case goods and each C7 depletion books its
    own TaxPaidRemoval. More accurate, defers the tax, and requires bonded case
    storage plus an opening physical count.

    At St. Amant's volume the whole year's excise is roughly $550 (first CBMA tier:
    $1.07 − $1.00 = $0.07/gal on col (a); $1.57 − $1.00 = $0.57 on col (b)), so this
    is a bookkeeping choice, not a cash one. Both paths are live; flip the constant.
    """
    from cellar.models import ConfigConstant
    row = ConfigConstant.objects.filter(key=REMOVAL_BASIS_KEY).first()
    return (row.value if row else "bottling").strip().lower()


def _apply_removal_basis(run, actor=None):
    """On the bottling basis, the run leaves bond the day it is bottled."""
    if removal_basis() != "bottling":
        return None
    from cellar.models import TaxPaidRemoval
    return TaxPaidRemoval.objects.create(
        bottling_run=run, removed_at=run.bottled_at,
        cases=run.cases_produced, channel=TaxPaidRemoval.Channel.OTHER)


def runs_for(lot):
    return (BottlingRun.objects.filter(source_lot=lot, voided_at__isnull=True)
            .select_related("bottle_format").order_by("-bottled_at"))
