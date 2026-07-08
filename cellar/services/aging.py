"""
Aging reports.

oak_summary  — weighted % by oak tier (volume × time), plus a per-placement detail.
composition_of — recursive leaf-lot decomposition, volume-weighted, following the
                 genealogy (blends, splits, and topping contributions) all the way down.
"""
from collections import defaultdict
from decimal import Decimal

from cellar.models import (AgingPlacement, LotLineage, VolumeMeasurement,
                           FortificationEvent, BookToBond)


class CompositionUnavailable(Exception):
    """Raised when a lot has contributions but no recorded produced volume,
    so its own-fruit share can't be honestly weighted."""


# ------------------------------------------------------------------ oak
def oak_detail(lot, asof=None):
    """Per-placement oak history for a lot."""
    rows = []
    for p in lot.placements.filter(voided_at__isnull=True).select_related("container").order_by("filled_at"):
        c = p.container
        rows.append({
            "container": c.container_id, "format": c.format or c.get_type_display(),
            "tier": p.get_oak_tier_display(), "forest": c.forest, "toast": c.toast,
            "volume_gal": float(p.volume_gal), "days": p.duration_days(asof),
            "filled": p.filled_at, "emptied": p.emptied_at,
        })
    return rows


def oak_summary(lot, asof=None):
    """Weighted % by oak tier: Σ(volume × days) per tier ÷ total volume-days."""
    weighted = defaultdict(float)
    total = 0.0
    for p in lot.placements.filter(voided_at__isnull=True):
        vd = float(p.volume_gal) * p.duration_days(asof)
        weighted[p.get_oak_tier_display()] += vd
        total += vd
    if not total:
        return {}
    return {tier: round(vd / total * 100, 1) for tier, vd in weighted.items()}


# ------------------------------------------------------------ composition
def _lot_volume(lot):
    """The lot's recorded produced volume — booking gauge, fortification, or book-to-bond."""
    vm = VolumeMeasurement.booking_volume_for(lot)
    if vm and vm.volume_gal:
        return float(vm.volume_gal)
    fe = lot.fortifications.filter(voided_at__isnull=True).order_by("-booked_at").first()
    if fe and fe.finished_wg:
        return float(fe.finished_wg)
    bb = lot.bond_bookings.filter(voided_at__isnull=True).order_by("-booked_at").first()
    if bb and bb.gallons_produced:
        return float(bb.gallons_produced)
    return None


_BLEND_ONLY = {LotLineage.Relationship.WHOLE_BLEND}


def composition_of(lot, _depth=0):
    """Return {leaf_lot_code: fraction} summing to 1, resolved to leaf lots."""
    inbound = list(LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True)
                   .select_related("parent_lot"))
    if not inbound or _depth > 25:
        return {lot.code: 1.0}

    total = _lot_volume(lot)
    if total is None:
        kinds = {e.relationship_type for e in inbound}
        if kinds <= _BLEND_ONLY:
            total = sum(float(e.volume_gal or 0) for e in inbound)  # pure blend: own = 0
        else:
            raise CompositionUnavailable(lot.code)

    comp = defaultdict(float)
    contributed = 0.0
    for edge in inbound:
        vol = float(edge.volume_gal or 0)
        contributed += vol
        for leaf, frac in composition_of(edge.parent_lot, _depth + 1).items():
            comp[leaf] += frac * vol

    own = (total - contributed) if total else 0.0
    if own > 0.001:
        comp[lot.code] += own

    s = sum(comp.values()) or 1.0
    return {k: v / s for k, v in comp.items()}


def composition_report(lot):
    """Composition as percentages, sorted high to low — the label/marketing record."""
    try:
        comp = composition_of(lot)
    except CompositionUnavailable:
        return {"⚠ no recorded produced volume — composition unavailable": None}
    return {k: round(v * 100, 2) for k, v in sorted(comp.items(), key=lambda kv: -kv[1])}


# ------------------------------------------------------ batch location
def racks_holding_lot(lot):
    from cellar.models import Rack
    return [r for r in Rack.objects.all() if any(l.id == lot.id for l in r.current_lots())]


def plan_batch_location(lot, location):
    """Batch-code a lot to a location, but never silently move a rack that can't
    honestly go there. Returns (clean, flagged):
      * clean   — racks holding only this lot, already at or unassigned to the location
      * flagged — [(rack, reason)] split racks (also hold another lot) or racks
                  currently recorded in a different location — verify before moving
    """
    clean, flagged = [], []
    for rack in racks_holding_lot(lot):
        lots = rack.current_lots()
        reasons = []
        if len(lots) > 1:
            others = ", ".join(l.code for l in lots if l.id != lot.id)
            reasons.append(f"split rack — also holds {others}")
        if rack.location_id and rack.location_id != location.id:
            reasons.append(f"currently recorded in {rack.location.code}")
        if reasons:
            flagged.append((rack, "; ".join(reasons)))
        else:
            clean.append(rack)
    return clean, flagged


def apply_batch_location(racks, location):
    for r in racks:
        r.location = location
        r.save(update_fields=["location"])
    return len(racks)
