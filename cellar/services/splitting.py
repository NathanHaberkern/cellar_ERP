"""
Partial transfer — moving SOME of a lot into another vessel.

`operations.transfer_lot()` moves a lot wholesale: it closes the open tank
assignment and opens a new one elsewhere. There was no way to move a PORTION of
a lot anywhere, which made the single most important thing Nate actually does
with the Verdelho impossible to record:

    9/3/25  RACK 250 gal to TOTE
            FORTIFY w/ 40 gal H. Proof

That 250 gal is not the same wine as the 715 gal left in SS-2 the moment the
spirit goes in. It's a different program (Port, not Table), it ends up in a
different tax class, and it gets its own bottling run. Recording it as a plain
transfer would have thrown away the 715 gal that stayed behind; recording it as
an addition would have left one lot pretending to be two wines at once.

So a partial transfer SPLITS: the portion that moves becomes its own lot, with
its own code, and a LotLineage edge ties it back to the parent.

    25VERD (965 gal, table)
      |-- SPLIT_DRAINOFF 250 gal --> 25VERDPORT (port) --> tote --> fortify
      `-- 715 gal remains in SS-2 -----------------------------> stays table

Two ways to name the child, both handled here:

  * SAME program as the parent (a plain partial rack into another tank, no
    program change) -> the child takes the next sequence on the parent's own
    abbreviation: 25VERD -> 25VERD2.
  * DIFFERENT program (the Port case) -> the abbreviation resolves through the
    curated VarietalDesignation catalog for (variety, new program), which is what
    turns VERD into VERDPORT. `generator.resolve_abbreviation()` already has the
    port fallback rule; this just calls it with the right program.

Volume is conserved exactly as bottling.create_parcel() does it: credit the
child, debit the parent, both as new append-only VolumeMeasurement rows. The
parent is NOT emptied out of its vessel unless the split took everything.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import Lot, LotLineage, TankAssignment, VolumeMeasurement
from cellar.models.base import LotKind, Program
from cellar.services import generator
from cellar.services import lotmeta
from cellar.services import operations as ops
from cellar.services import volumes as vol_svc

GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v not in (None, "") else None


@transaction.atomic
def split_lot(parent, *, volume_gal, to_vessel, at=None, program=None,
              note="", actor=None):
    """Move `volume_gal` of `parent` into `to_vessel` as a NEW lot. Returns the
    child Lot.

    program : the child's program. None (default) keeps the parent's — an
              ordinary partial rack. Program.PORT is the fortification case and
              produces the PORT-suffixed code (25VERD -> 25VERDPORT).

    Raises if the volume exceeds what the parent actually holds, or if the
    destination vessel is occupied by a different lot (use the Blend workflow
    for that — combining two wines has to write blend lineage).
    """
    at = at or timezone.now()
    vol = _d(volume_gal)
    if vol is None or vol <= 0:
        raise ValueError("Enter the gallons to move.")

    available = vol_svc.working_volume(parent)
    if available is None:
        raise ValueError(
            f"{parent.code} has no recorded volume yet — gauge it before splitting.")
    if vol > available:
        raise ValueError(
            f"{parent.code} holds {available:g} gal — can't move {vol:g} gal out of it.")

    # Occupancy: same rule as transfer_lot. A partial rack into a tank that already
    # holds a different wine is a BLEND, and blends must go through the blend
    # workflow so the lineage edges get written.
    occ = ops.open_assignment_for(to_vessel)
    if occ is not None and occ.lot_id != parent.id:
        raise ValueError(
            f"{to_vessel.code} already holds {occ.lot.code}. Combining two wines has "
            "to go through the Blend workflow so the lineage is recorded.")

    parent_program = lotmeta.lot_program(parent)
    child_program = program or parent_program or Program.TABLE
    block = lotmeta.lot_block(parent)

    # The variety normally resolves through the curated VarietalDesignation catalog
    # (abbr -> variety+program). Two ways that comes back empty, and neither may
    # fall through to a meaningless "_P" suffix — this is exactly the Verdelho case,
    # and naming the Port parcel 25VERD_P instead of 25VERDPORT would be wrong:
    #   1. The parent's code was autofired PROVISIONALLY (no curated catalog row
    #      yet — the common case mid-first-season), so there's nothing to resolve
    #      back through. Come at the variety off the fruit instead
    #      (weigh tag -> harvest event -> block.variety).
    #   2. The parent has no weigh-tag allocation either (hand-created lot). Then
    #      derive the abbreviation straight from the PARENT'S abbreviation, which
    #      is the same rule generator.resolve_abbreviation() already uses for its
    #      port fallback: <table code> + PORT.
    variety = lotmeta.lot_variety(parent)
    if variety is None and block is not None:
        variety = block.variety

    child = Lot.objects.create(
        vintage_year=parent.vintage_year,
        status=parent.status,
        production_intent=parent.production_intent)

    if variety is not None:
        generator.assign_initial_designation(
            child, variety, child_program,
            block=block,
            vineyard=block.vineyard if block is not None else None)
    else:
        _designate_from_parent_abbr(child, parent, child_program)

    from cellar.services import costing as costing_svc
    _cpg = costing_svc.parent_cost_per_gal(parent)   # before the edge moves volume

    LotLineage.objects.create(
        parent_lot=parent, child_lot=child,
        relationship_type=LotLineage.Relationship.SPLIT_DRAINOFF,
        volume_gal=vol,
        occurred_at=costing_svc.to_business_date(at), cost_per_gal_snapshot=_cpg)

    # NOTE: no VolumeMeasurement is written for either lot. SPLIT_DRAINOFF is a
    # LIQUID edge (volumes._LIQUID_EDGES), so lot_balance() already reads this one
    # edge as outbound on the parent and inbound on the child — the volume moves
    # by virtue of the edge existing. Writing gauges on top of that would credit
    # the child twice (250 gal booked + 250 gal inbound = a 500 gal parcel, which
    # is precisely the bug this comment is here to prevent). Contrast
    # bottling.create_parcel(), which DOES write gauges — correctly, because
    # BOTTLING_SPLIT is deliberately excluded from _LIQUID_EDGES.

    # The parent only leaves its vessel if the split took everything.
    remaining = (available - vol).quantize(GAL)
    if remaining <= 0:
        (TankAssignment.objects
         .filter(lot=parent, voided_at__isnull=True, emptied_at__isnull=True)
         .update(emptied_at=at))

    assignment = ops.assign_lot_to_vessel(child, to_vessel, at)
    if note:
        TankAssignment.objects.filter(pk=assignment.pk).update(notes=note)

    return child


def _designate_from_parent_abbr(child, parent, child_program):
    """Name the child off the parent's abbreviation when no Variety resolves.

    25VERD split to Port -> VERDPORT -> 25VERDPORT. Same program -> the parent's
    own abbreviation, taking the next sequence (25VERD -> 25VERD2). Flagged
    provisional, because it was inferred rather than read from the curated catalog.
    """
    from cellar.models import LotDesignation
    from cellar.models.base import LotKind

    parent_abbrs = lotmeta._abbrs(parent)
    base = parent_abbrs[0] if parent_abbrs else "LOT"
    if child_program == Program.PORT and not base.endswith("PORT"):
        abbr = f"{base}PORT"
    else:
        abbr = base

    seq = generator.next_sequence(child.vintage_year, abbr)
    d = LotDesignation.objects.create(
        lot=child, kind=LotKind.STANDARD,
        members=[{"abbr": abbr, "seq": seq}], is_provisional=True)
    child.current_designation = d
    child.save(update_fields=["current_designation"])
    return d


def splits_of(lot):
    """Partial-transfer children of this lot, newest first."""
    edges = (LotLineage.objects
             .filter(parent_lot=lot, voided_at__isnull=True,
                     relationship_type=LotLineage.Relationship.SPLIT_DRAINOFF)
             .select_related("child_lot__current_designation")
             .order_by("-created_at"))
    return [{"lot": e.child_lot, "volume_gal": e.volume_gal} for e in edges]
