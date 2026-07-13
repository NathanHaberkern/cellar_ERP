"""
Blending — a deliberate merge of one or more source lots into a destination lot.

WHY THIS MODULE EXISTS
-----------------------
`LotLineage.Relationship` has carried WHOLE_BLEND and PARTIAL_BLEND since the
lineage model was written, and three different readers already depend on them:

  * volumes.py     — inbound/outbound liquid edges, so a blend correctly debits
                      the parent's balance and credits the child's.
  * partx.py       — the change-of-tax-class narrative (footnote 5): blending
                      across tax classes is reported as used-by-blending on the
                      parent's line and produced-by-blending on the child's.
  * lotpages.py     — the Movement timeline ("Blending" rows) and composition_of()
                      (the genealogy percentages on the Composition tab).

Nothing ever wrote one. `transfer_lot(..., allow_blend=True)` lets two lots
co-occupy a vessel, but that's a tank-map exception, not a compliance record —
no lineage edge, no gallons, nothing for partx.py or composition to read. This
closes that gap: it is the write path the readers were already built for.

TWO SHAPES
----------
WHOLE_BLEND      — the parent's ENTIRE current balance moves into the child.
                   The parent lot doesn't cease to exist as a row, but it stops
                   holding its own wine — composition_of() reports it purely as
                   a lineage contributor from here on (own = 0).
PARTIAL_BLEND    — only `volume_gal` of the parent's balance moves; the parent
                   keeps the remainder as its own wine, unblended.

Both are driven by the same function, `blend()`, called once per source lot
against one destination. A blend of N source lots into one destination is N
calls sharing a `blended_at` — the caller (web layer) loops.

WINE MOVEMENT
-------------
The destination lot is not necessarily new — it is usually an existing lot
(the blend target) or the vessel the sources are being combined into. This
module does not create lots or vessels; it debits/credits balances via the
lineage edge and, when `to_vessel` is given, also books the physical move
through `operations.transfer_lot(..., allow_blend=True)` so the tank map
reflects reality. If the source is being fully emptied (WHOLE_BLEND or a
PARTIAL_BLEND that happens to drain it), its own tank assignment is closed
the same way rack_out() closes one — by stamping `emptied_at`, not by
deleting anything.

TAX CLASS
---------
Blending across tax classes is legal and exactly what footnote 5/ of the
5120.17 exists to report — see partx.py. This module does not block it. The
web layer checks classes first and asks for confirmation if they differ;
by the time `blend()` is called, that's already been decided.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import Lot, LotLineage, TankAssignment
from cellar.services import operations as ops
from cellar.services import volumes as vol_svc

GAL = Decimal("0.1")
ZERO = Decimal("0")


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v not in (None, "") else None


class InsufficientWine(ValueError):
    """The source lot doesn't hold enough wine to cover this blend."""


def tax_class_of(lot):
    from cellar.services.reporting import lot_tax_class
    return lot_tax_class(lot)


def source_balance(lot):
    """What's available to blend out of this lot right now."""
    bal = vol_svc.lot_balance(lot)
    return bal if bal is not None else ZERO


def preview(source_lot, dest_lot, *, kind, volume_gal=None):
    """What a blend would do, before committing — the confirm-screen numbers.

    Returns balances before/after and whether the two lots' tax classes match,
    so the web layer can decide whether to show the cross-class warning.
    """
    bal = source_balance(source_lot)
    vol = bal if kind == LotLineage.Relationship.WHOLE_BLEND else _d(volume_gal)
    if vol is None:
        vol = ZERO
    src_class = tax_class_of(source_lot)
    dst_class = tax_class_of(dest_lot)
    return {
        "source_balance": bal,
        "volume": vol,
        "source_remaining": (bal - vol).quantize(GAL),
        "dest_balance_before": source_balance(dest_lot),
        "source_tax_class": src_class,
        "dest_tax_class": dst_class,
        "class_mismatch": src_class != dst_class,
        "sufficient": vol <= bal,
    }


@transaction.atomic
def blend(source_lot, dest_lot, *, blended_at, kind=LotLineage.Relationship.WHOLE_BLEND,
          volume_gal=None, to_vessel=None, allow_overdraw=False, actor=None):
    """Blend `source_lot` into `dest_lot`.

    kind        : WHOLE_BLEND (moves the source's entire current balance) or
                  PARTIAL_BLEND (moves exactly `volume_gal`).
    volume_gal  : required for PARTIAL_BLEND; ignored (computed) for WHOLE_BLEND.
    to_vessel   : if given, also books the physical move — closes the source's
                  open tank assignment and opens/co-occupies `to_vessel` for the
                  destination lot (allow_blend=True, so co-occupancy is allowed).
                  Leave blank if the physical move already happened separately
                  (e.g. wine was racked first, this call is just the paperwork).

    Returns the LotLineage edge.
    """
    if source_lot.pk == dest_lot.pk:
        raise ValueError("A lot can't be blended into itself.")

    bal = source_balance(source_lot)

    if kind == LotLineage.Relationship.WHOLE_BLEND:
        vol = bal
    else:
        vol = _d(volume_gal)
        if vol is None or vol <= 0:
            raise ValueError("Enter the gallons to blend for a partial blend.")

    if vol <= 0:
        raise ValueError(f"{source_lot.code} has no wine to blend ({bal} gal on hand).")
    if vol > bal and not allow_overdraw:
        raise InsufficientWine(
            f"{source_lot.code} holds {bal} gal; you're blending {vol} gal. "
            f"Check the source balance, or gauge the lot before blending.")

    edge = LotLineage.objects.create(
        parent_lot=source_lot, child_lot=dest_lot,
        relationship_type=kind, volume_gal=vol)

    if to_vessel is not None:
        # Close the source's own tank assignment — its wine (all or in part) has
        # left it and is now the destination's, physically as well as on paper.
        (TankAssignment.objects
         .filter(lot=source_lot, voided_at__isnull=True, emptied_at__isnull=True)
         .update(emptied_at=blended_at))
        ops.assign_lot_to_vessel(dest_lot, to_vessel, blended_at, allow_blend=True)

    return edge


def blend_many(sources, dest_lot, *, blended_at, to_vessel=None, actor=None):
    """Blend several sources into one destination in a single call.

    sources : iterable of (lot, kind, volume_gal) — volume_gal ignored for
              WHOLE_BLEND. All-or-nothing: if any source can't cover its draw,
              nothing is written.
    """
    with transaction.atomic():
        edges = []
        for lot, kind, vol in sources:
            edges.append(blend(
                lot, dest_lot, blended_at=blended_at, kind=kind,
                volume_gal=vol, to_vessel=to_vessel, actor=actor))
        return edges
