"""
Running volume balances.

The system had no lot balance. `_lot_volume()` returns the BOOKED production
volume (the compliance figure — booking gauge, fortification T, or book-to-bond),
and `current_volume()` returns the latest gauge. Neither one moves when wine
leaves the lot. That was survivable while the only operations were intake and
racking; it is not survivable once wine can be drawn out to top another lot,
blended away, or bottled — you cannot top 60 barrels from a lot without knowing
whether the lot still has 60 gallons in it.

So: one place that answers "how much wine is actually in this lot right now".

    balance = booked
              + inbound lineage   (wine given to this lot — topping, blend)
              - outbound lineage  (wine this lot gave away)
              - volume losses     (evaporation, spillage, accrued angel's share)
              - bulk taxpaid removals
              - bottled gallons

DOUBLE-COUNTING, and why the source debit is a lineage edge and not a loss
--------------------------------------------------------------------------
Nate's rule: "if you have 61 barrels of wine A and you use 1 to top the other 60,
you have a net loss of 1 barrel." True — and the loss is the EVAPORATION, booked
once. `ToppingTarget.save()` already books a VolumeLoss on the TARGET lot equal to
the wine added (routine topping refills exactly what evaporated).

  * source == target (lot A tops its own barrels): that VolumeLoss is the whole
    debit. Booking a second row for the draw would double-debit lot A, and would
    double-report the loss on 5120.17 line A30.
  * source != target: the target's evaporation is a real loss; the wine that came
    from the source is a TRANSFER, not a loss. It leaves the source and arrives in
    the target. `ToppingTarget.save()` already writes that as a LotLineage
    TOPPING edge — which this function reads as outbound on the source and inbound
    on the target.

Net cellar-wide: total wine drops by evaporation only, and it drops on the lot
that actually evaporated it. Nobody's loss line is inflated.
"""
from decimal import Decimal

from cellar.models import (
    AgingPlacement, BottlingRun, BulkTaxPaidRemoval, LotLineage, VolumeLoss,
    BondTransfer, MustSale,
)
from cellar.services.aging import _lot_volume

ZERO = Decimal("0")
GAL = Decimal("0.1")

# Lineage edges that move liquid. BOTTLING_SPLIT does too, but a bottling parcel
# is accounted through BottlingRun, so counting the split edge as well would
# debit the parent twice.
_LIQUID_EDGES = {
    LotLineage.Relationship.WHOLE_BLEND,
    LotLineage.Relationship.PARTIAL_BLEND,
    LotLineage.Relationship.TOPPING,
    LotLineage.Relationship.SPLIT_SAIGNEE,
    LotLineage.Relationship.SPLIT_DRAINOFF,
}


def _d(v):
    return Decimal(str(v)) if v is not None else ZERO


def booked_volume(lot):
    """The compliance production figure, or None if the lot hasn't booked yet."""
    v = _lot_volume(lot)
    return None if v is None else _d(v)


def inbound_gal(lot):
    return sum((_d(e.volume_gal) for e in
                LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True,
                                          relationship_type__in=_LIQUID_EDGES)), ZERO)


def outbound_gal(lot):
    return sum((_d(e.volume_gal) for e in
                LotLineage.objects.filter(parent_lot=lot, voided_at__isnull=True,
                                          relationship_type__in=_LIQUID_EDGES)), ZERO)


def losses_gal(lot):
    return sum((_d(l.volume_gal) for l in
                VolumeLoss.objects.filter(lot=lot, voided_at__isnull=True)), ZERO)


def bottled_gal(lot):
    total = ZERO
    for run in BottlingRun.objects.filter(source_lot=lot, voided_at__isnull=True):
        total += _d(run.volume_bottled_gal)
        loss = run.bottling_loss_gal
        if loss and loss > 0:
            total += _d(loss)
    return total


def bulk_removed_gal(lot):
    return sum((_d(r.wine_gallons) for r in
                BulkTaxPaidRemoval.objects.filter(lot=lot, voided_at__isnull=True)), ZERO)


def bond_transferred_out_gal(lot):
    """BondTransfer OUT rows tied to this lot. The docstring formula at the top
    of this file already listed 'bulk taxpaid removals' as a balance deduction
    but not in-bond transfers out — an oversight, since both are wine actually
    leaving the lot. Fixed here."""
    return sum((_d(t.gallons) for t in
                BondTransfer.objects.filter(
                    lot=lot, direction=BondTransfer.Direction.OUT,
                    voided_at__isnull=True)), ZERO)


def must_sold_gal(lot):
    """Bulk juice/must sold before the lot was ever inoculated — see
    services/external_transfer.py and models.MustSale."""
    return sum((_d(s.gallons) for s in
                MustSale.objects.filter(lot=lot, voided_at__isnull=True)), ZERO)


def volume_added_gal(lot):
    """Liquid ADDED to the lot: water, and sweetening concentrate.

    This has to be an explicit balance term, and getting that wrong is subtle.
    The instinct is to record a water addition by writing a new, larger
    VolumeMeasurement — but a gauge is not liquid. `booked_volume()` reads the
    ONE highest-confidence gauge (or the flagged booking gauge); it does not sum
    them. So a fresh "the tank now reads 957" row is simply ignored by the
    balance, and the addition silently does nothing. Worse, if such a row ever
    DID win the confidence race it would become `booked` — and then every
    removal already netted out of the balance would be subtracted a second time.

    So liquid in is counted here, once, from the events that actually put it
    there — symmetrically with how bottling/losses/removals count liquid out.
    """
    from cellar.models import Addition, SweeteningEvent, Additive
    water = sum((_d(a.quantity) for a in
                 Addition.objects.filter(lot=lot, voided_at__isnull=True,
                                         additive__dose_mode=Additive.DoseMode.PCT_VOLUME)
                 .select_related("additive")), ZERO)
    sweetening = sum((_d(s.concentrate_gallons) for s in
                      SweeteningEvent.objects.filter(lot=lot, voided_at__isnull=True)), ZERO)
    return water + sweetening


def lot_balance(lot):
    """Wine (or juice/must, pre-ferment) currently in the lot, in gallons. None
    if the lot has never booked a volume AND has no inbound liquid (i.e.
    nothing to balance yet)."""
    booked = booked_volume(lot)
    inbound = inbound_gal(lot)
    if booked is None and inbound == ZERO:
        return None
    bal = ((booked or ZERO) + inbound + volume_added_gal(lot)
           - outbound_gal(lot) - losses_gal(lot)
           - bottled_gal(lot) - bulk_removed_gal(lot)
           - bond_transferred_out_gal(lot) - must_sold_gal(lot))
    return bal.quantize(GAL)


def lot_balance_detail(lot):
    """The same figure, itemised — for the UI and for arguing with the numbers."""
    booked = booked_volume(lot)
    return {
        "booked": booked,
        "inbound": inbound_gal(lot).quantize(GAL),
        "outbound": outbound_gal(lot).quantize(GAL),
        "losses": losses_gal(lot).quantize(GAL),
        "bottled": bottled_gal(lot).quantize(GAL),
        "bulk_removed": bulk_removed_gal(lot).quantize(GAL),
        "volume_added": volume_added_gal(lot).quantize(GAL),
        "bond_transferred_out": bond_transferred_out_gal(lot).quantize(GAL),
        "must_sold": must_sold_gal(lot).quantize(GAL),
        "balance": lot_balance(lot),
    }


def working_volume(lot):
    """"How much is actually in this lot right now" — the ONE number the UI shows
    and the dose math bases on.

    This is `lot_balance()` (booked + inbound − everything that left), falling
    back to the latest raw gauge only when the lot has nothing to balance yet
    (no booked volume and no inbound liquid — e.g. a lot that's been crushed but
    not yet gauged).

    It exists because `operations.current_volume()` returns the LATEST GAUGE,
    which is a different question. A gauge is a snapshot of a measurement that
    was taken; it doesn't move when wine is sold, transferred out in bond,
    bottled, or lost. That's fine for "what did the last reading say" but wrong
    for "what's in the tank" — and it's why a 100 gal must sale and a B2B
    transfer both left the displayed volume untouched. Anything that means the
    latter should call this, not current_volume().
    """
    from cellar.services.operations import current_volume
    bal = lot_balance(lot)
    return bal if bal is not None else current_volume(lot)


# ======================================================================
# Container-level
# ======================================================================
DEFAULT_HEADSPACE = Decimal("3")


def placement_capacity(placement):
    """Working capacity of the container holding this placement — the nominal
    capacity less headspace. Barrels are 60 / 70 / 130 gal; Titan is 550 and the
    SS totes are 450 (portable, and routinely not filled to the top)."""
    cap = _d(placement.container.capacity_gal)
    return (cap - DEFAULT_HEADSPACE) if cap > DEFAULT_HEADSPACE else cap


def placement_volume(placement):
    """Current wine in this container: filled volume, less anything topped out of
    it, plus anything topped into it."""
    vol = _d(placement.volume_gal)
    for t in placement.toppings.filter(voided_at__isnull=True):
        vol += _d(t.volume_added) - _d(t.evaporative_loss)
    return vol.quantize(GAL)


def is_partial(placement):
    """A barrel that came off the fill line short. Not empty — it holds wine, and
    the container is unavailable — but it is not full either, so it must be
    flagged and topped up from another wine. Computed, not stored: the fill volume
    and the container capacity already say everything.

    Tolerance of 1 gal: a 57.4-gallon fill of a 60-gal barrel with 3 gal headspace
    is a full barrel, not a partial one.
    """
    if placement.emptied_at is not None:
        return False
    cap = placement_capacity(placement)
    if cap <= 0:
        return False
    return placement_volume(placement) < (cap - Decimal("1"))


def ullage(placement):
    """Gallons needed to bring a partial barrel up to full."""
    gap = placement_capacity(placement) - placement_volume(placement)
    return gap.quantize(GAL) if gap > 0 else ZERO


def partial_placements(lot=None):
    qs = AgingPlacement.objects.filter(emptied_at__isnull=True, voided_at__isnull=True)
    if lot is not None:
        qs = qs.filter(lot=lot)
    return [p for p in qs.select_related("container", "lot") if is_partial(p)]
