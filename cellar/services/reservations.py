"""Reserving wine for a buyer, a future blend, or a program.

WHAT PROBLEM THIS SOLVES
------------------------
Before this, nothing in the system could say "this wine is spoken for" while it
was still in inventory. `ExternalDestination` exists, but it is only ever
attached at the moment wine LEAVES (BulkTaxPaidRemoval, external transfers) — it
records where wine went, not where it is earmarked to go. `TankAssignment` and
`RackAssignment` are physical placement only. `Lot.program` is TTB tax class, a
different axis entirely.

So a commitment lived in someone's head, or in a lot-code naming convention.
The 2019 Naked Wines bulk blends (TN_NW / TEMP_NW) are exactly that: the only
record that 6,100 gallons were committed was a suffix on the lot code.

GRANULARITY
-----------
See LotReservation's docstring. Briefly: the lot is always named, the barrel
optionally. Barrel-level matters because barrel identity is not incidental —
Container carries cooper/forest/toast/grain, and selecting barrels for a reserve
bottling is the whole practice.

CLOSING
-------
A reservation closes one of two ways:

  released  — you changed your mind. Explicit, recorded, never deleted.
  satisfied — the wine was blended away. This is NOT inferred from timestamps.
              A barrel-level reservation is satisfied when a LineageSource row
              names its placement; a lot-level one when a blend edge drains the
              lot. Before LineageSource existed, the only way to link "these 12
              barrels were racked out at 9am" to "a blend happened at 10am" was
              proximity in time, which is fine when you remember the vintage and
              worthless in an audit three years later.

OVERCOMMITMENT IS WARNED, NOT BLOCKED
--------------------------------------
Reserving more than a lot holds, or reserving the same barrel twice, returns
warnings and writes anyway. Deliberate: during blending trials you legitimately
want tentative holds on overlapping barrel sets, and a hard block would push
that work back out of the system into a spreadsheet — which is the failure mode
this module exists to end.
"""
from decimal import Decimal

from django.db import transaction

from cellar.models import AgingPlacement, LineageSource, LotLineage, LotReservation
from cellar.services import volumes as vol_svc

ZERO = Decimal("0")
GAL = Decimal("0.1")

# Blend edges that consume the parent's wine. A TOPPING or PARTIAL_FILL edge
# draws from the source too, but drawing a few gallons to fill someone else's
# barrel does not fulfil a commitment to deliver this wine — those are not
# satisfying events.
_SATISFYING_EDGES = {
    LotLineage.Relationship.WHOLE_BLEND,
    LotLineage.Relationship.PARTIAL_BLEND,
}


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v is not None else None


def open_reservations(lot=None, placement=None):
    """Currently-open reservations, optionally narrowed to a lot or barrel."""
    qs = (LotReservation.objects
          .filter(voided_at__isnull=True, released_at__isnull=True, satisfied_at__isnull=True)
          .select_related("lot", "placement__container", "destination"))
    if lot is not None:
        qs = qs.filter(lot=lot)
    if placement is not None:
        qs = qs.filter(placement=placement)
    return qs


def reserved_gal(lot):
    """Gallons of `lot` currently spoken for.

    A whole-lot reservation (no placement, no volume) counts as the lot's
    entire current balance; a barrel-level one counts that barrel's volume.
    """
    total = ZERO
    for r in open_reservations(lot=lot):
        if r.volume_gal is not None:
            total += r.volume_gal
        elif r.placement is not None:
            total += vol_svc.placement_volume(r.placement) or ZERO
        else:
            total += vol_svc.lot_balance(lot) or ZERO
    return total


def check(lot, *, placement=None, volume_gal=None):
    """Warnings for a proposed reservation. Never raises — see module docstring."""
    warnings = []

    if placement is not None:
        if placement.lot_id != lot.pk:
            warnings.append(
                f"{placement.container} holds {placement.lot.code}, not {lot.code}.")
        if placement.emptied_at is not None:
            warnings.append(f"{placement.container} was emptied on {placement.emptied_at}.")
        existing = open_reservations(placement=placement)
        if existing.exists():
            who = ", ".join(str(r.destination or r.label or r.get_purpose_display())
                            for r in existing)
            warnings.append(f"{placement.container} is already reserved for {who}.")

    balance = vol_svc.lot_balance(lot) or ZERO
    already = reserved_gal(lot)
    proposed = _d(volume_gal) or ZERO
    if placement is not None:
        proposed = vol_svc.placement_volume(placement) or ZERO
    elif volume_gal is None:
        proposed = balance

    if already + proposed > balance:
        warnings.append(
            f"{lot.code} holds {balance} gal; this would put {already + proposed} gal "
            f"under reservation.")
    return warnings


@transaction.atomic
def reserve(lot, *, reserved_at, placement=None, purpose=LotReservation.Purpose.SALE,
            destination=None, label="", volume_gal=None, notes="", actor=None):
    """Reserve wine. Returns (reservation, warnings)."""
    if placement is not None and not isinstance(placement, AgingPlacement):
        placement = AgingPlacement.objects.select_related("container", "lot").get(pk=placement)
    if placement is not None and volume_gal is not None:
        raise ValueError(
            "A barrel reservation takes its volume from the barrel — don't pass volume_gal.")
    if destination is None and not label and purpose == LotReservation.Purpose.SALE:
        raise ValueError("A sale reservation needs a buyer or at least a program label.")

    warnings = check(lot, placement=placement, volume_gal=volume_gal)
    res = LotReservation.objects.create(
        lot=lot, placement=placement, purpose=purpose, destination=destination,
        label=label, volume_gal=_d(volume_gal), reserved_at=reserved_at,
        notes=notes, operator=actor)
    return res, warnings


@transaction.atomic
def release(reservation, *, released_at):
    """Give the wine back — the commitment ended without being fulfilled."""
    if not reservation.is_open:
        raise ValueError("That reservation is already closed.")
    reservation.released_at = released_at
    reservation.save(update_fields=["released_at"])
    return reservation


@transaction.atomic
def settle_for_edge(edge):
    """Close reservations the blend on `edge` has fulfilled.

    Called from the blend workflow after the edge and its LineageSource rows
    exist. Returns the reservations closed.

    Barrel-level: closed when this edge names the barrel in LineageSource. If a
    partial blend carries no barrel detail, barrel reservations are LEFT OPEN
    rather than guessed at — silently discarding a real commitment is worse
    than leaving one to be cleared by hand.

    Lot-level: closed when the blend drains the lot. A partial blend that
    leaves wine behind hasn't fulfilled a whole-lot commitment.
    """
    if edge.relationship_type not in _SATISFYING_EDGES:
        return []

    closed = []
    when = edge.occurred_at

    named = set(LineageSource.objects
                .filter(lineage=edge, voided_at__isnull=True)
                .values_list("placement_id", flat=True))
    for res in open_reservations(lot=edge.parent_lot):
        if res.placement_id is not None:
            hit = res.placement_id in named
        else:
            hit = (vol_svc.lot_balance(edge.parent_lot) or ZERO) <= GAL
        if hit:
            res.satisfied_at = when
            res.save(update_fields=["satisfied_at"])
            closed.append(res)
    return closed


def unsettled_after_blend(edge):
    """Barrel reservations still open on a lot that was just partially blended
    without barrel detail — the case settle_for_edge() deliberately won't guess.
    The caller surfaces this so it gets cleared deliberately rather than rotting.
    """
    if edge.relationship_type != LotLineage.Relationship.PARTIAL_BLEND:
        return []
    if LineageSource.objects.filter(lineage=edge, voided_at__isnull=True).exists():
        return []
    return list(open_reservations(lot=edge.parent_lot).filter(placement__isnull=False))
