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


# --- as-of filtering ---------------------------------------------------------
# Every balance component gained an optional `as_of` so overhead allocation can ask
# "how many gallons were in this lot on 31 October" and get a reproducible answer
# months later. Threading it through here rather than reimplementing lot_balance in
# the overhead service is deliberate: a second copy of this arithmetic would drift
# from this one, and the drift would be invisible.
#
# Filtering happens in Python, not the DB, for two reasons: LotLineage.occurred_at
# is nullable (pre-0027 edges fall back to created_at, which no DB filter can
# express cleanly), and these per-lot querysets are tiny. Uniformity beats a
# micro-optimisation nobody will ever measure.
def _on_or_before(obj, as_of, *attrs):
    """True if the first non-null date attr on `obj` is on or before `as_of`."""
    if as_of is None:
        return True
    from cellar.services.costing import to_business_date
    for a in attrs:
        d = to_business_date(getattr(obj, a, None))
        if d is not None:
            return d <= as_of
    return True          # undated rows are treated as always-present


def _keep(rows, as_of, *attrs):
    return [o for o in rows if _on_or_before(o, as_of, *attrs)]


def booked_volume(lot, as_of=None):
    """The compliance production figure, or None if the lot hasn't booked yet.

    With `as_of`, returns None when the lot had not booked by that date — wine that
    did not yet exist cannot absorb overhead.
    """
    v = _lot_volume(lot)
    if v is None:
        return None
    if as_of is not None:
        booked_on = _booked_on(lot)
        if booked_on is not None and booked_on > as_of:
            return None
    return _d(v)


def _booked_on(lot):
    """The date a lot's production volume came into existence.

    compliance_ledger._booking_date() covers BookToBond and FortificationEvent, but
    a Verdelho that goes tank-gauge -> bottle has neither — it books off a
    VolumeMeasurement alone. Without the gauge fallback such a lot has no booking
    date, is treated as always-present, and absorbs overhead for months before the
    fruit was picked.
    """
    from cellar.models import VolumeMeasurement
    from cellar.services.compliance_ledger import _booking_date
    from cellar.services.costing import to_business_date

    d = to_business_date(_booking_date(lot))
    if d is not None:
        return d
    m = (VolumeMeasurement.objects.filter(lot=lot, voided_at__isnull=True)
         .order_by("measured_at").values_list("measured_at", flat=True).first())
    return to_business_date(m)


def inbound_gal(lot, as_of=None):
    rows = LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True,
                                     relationship_type__in=_LIQUID_EDGES)
    return sum((_d(e.volume_gal) for e in
                _keep(rows, as_of, "occurred_at", "created_at")), ZERO)


def outbound_gal(lot, as_of=None):
    rows = LotLineage.objects.filter(parent_lot=lot, voided_at__isnull=True,
                                     relationship_type__in=_LIQUID_EDGES)
    return sum((_d(e.volume_gal) for e in
                _keep(rows, as_of, "occurred_at", "created_at")), ZERO)


def losses_gal(lot, as_of=None):
    rows = VolumeLoss.objects.filter(lot=lot, voided_at__isnull=True)
    return sum((_d(l.volume_gal) for l in _keep(rows, as_of, "occurred_at")), ZERO)


def bottled_gal(lot, as_of=None):
    total = ZERO
    rows = BottlingRun.objects.filter(source_lot=lot, voided_at__isnull=True)
    for run in _keep(rows, as_of, "bottled_at"):
        total += _d(run.volume_bottled_gal)
        loss = run.bottling_loss_gal
        if loss and loss > 0:
            total += _d(loss)
    return total


def bulk_removed_gal(lot, as_of=None):
    rows = BulkTaxPaidRemoval.objects.filter(lot=lot, voided_at__isnull=True)
    return sum((_d(r.wine_gallons) for r in _keep(rows, as_of, "removed_at")), ZERO)


def bond_transferred_out_gal(lot, as_of=None):
    """BondTransfer OUT rows tied to this lot. The docstring formula at the top
    of this file already listed 'bulk taxpaid removals' as a balance deduction
    but not in-bond transfers out — an oversight, since both are wine actually
    leaving the lot. Fixed here."""
    rows = BondTransfer.objects.filter(lot=lot, direction=BondTransfer.Direction.OUT,
                                       voided_at__isnull=True)
    return sum((_d(t.gallons) for t in _keep(rows, as_of, "transferred_at")), ZERO)


def must_sold_gal(lot, as_of=None):
    """Bulk juice/must sold before the lot was ever inoculated — see
    services/external_transfer.py and models.MustSale."""
    rows = MustSale.objects.filter(lot=lot, voided_at__isnull=True)
    return sum((_d(s.gallons) for s in _keep(rows, as_of, "sold_at")), ZERO)


def volume_added_gal(lot, as_of=None):
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
    w_rows = (Addition.objects.filter(lot=lot, voided_at__isnull=True,
                                      additive__dose_mode=Additive.DoseMode.PCT_VOLUME)
              .select_related("additive"))
    water = sum((_d(a.quantity) for a in _keep(w_rows, as_of, "added_at")), ZERO)
    s_rows = SweeteningEvent.objects.filter(lot=lot, voided_at__isnull=True)
    sweetening = sum((_d(s.concentrate_gallons) for s in
                      _keep(s_rows, as_of, "sweetened_at")), ZERO)
    return water + sweetening


def lot_balance(lot, as_of=None):
    """Wine (or juice/must, pre-ferment) currently in the lot, in gallons. None
    if the lot has never booked a volume AND has no inbound liquid (i.e.
    nothing to balance yet)."""
    booked = booked_volume(lot, as_of)
    inbound = inbound_gal(lot, as_of)
    if booked is None and inbound == ZERO:
        return None
    bal = ((booked or ZERO) + inbound + volume_added_gal(lot, as_of)
           - outbound_gal(lot, as_of) - losses_gal(lot, as_of)
           - bottled_gal(lot, as_of) - bulk_removed_gal(lot, as_of)
           - bond_transferred_out_gal(lot, as_of) - must_sold_gal(lot, as_of))
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
