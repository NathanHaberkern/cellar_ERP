"""
Barrel depreciation → wine, via custody intervals.

Design (locked with Nate):
  * 50 / 33 / 17 of landed cost across the barrel's first three age-years, then $0.
  * A barrel-year's depreciation slice is allocated across the wines that held the
    barrel that year, weighted by CUSTODY time, where custody runs fill-to-next-fill.
    So turnaround idle (a wine racked out in July, next fill in September) belongs to
    the DEPARTING wine — the barrel couldn't have held anything else in the gap.
  * Trailing idle (last wine of the year → year-end) → that last wine.
  * Leading idle (year-start → first fill, when the barrel started empty) → the first wine.
  * A barrel-year with no wine at all → unallocated (idle-capacity overhead, not capitalized).

Custody intervals tile the whole barrel-year with no gaps, so no depreciation
falls on the floor.
"""
from collections import defaultdict
from datetime import timedelta

DEP_WEIGHTS = (0.50, 0.33, 0.17)   # config: barrel-year 1/2/3; year 4+ = 0


def _add_years(d, n):
    """Calendar-accurate year offset (Feb 29 → Feb 28)."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def barrel_depreciation_by_lot(container, weights=DEP_WEIGHTS):
    """Returns ({lot: dollars}, overhead_dollars) for one barrel across its whole life."""
    result = defaultdict(float)
    overhead = 0.0
    if not container.is_oak:
        return result, overhead
    cost = container.landed_cost_usd()
    if not cost:
        return result, overhead
    cost = float(cost)

    placements = list(container.placements.filter(voided_at__isnull=True).order_by("filled_at"))
    if not placements:
        return result, overhead          # never filled → not yet in service
    in_service = placements[0].filled_at

    for k, w in enumerate(weights, start=1):
        ys = _add_years(in_service, k - 1)
        ye = _add_years(in_service, k)
        slice_amt = cost * w
        year_days = (ye - ys).days

        # wine occupying the barrel at year-start (carries in from a prior year)
        carry = None
        for p in placements:
            if p.filled_at < ys and (p.emptied_at is None or p.emptied_at > ys):
                carry = p
        fills = [p for p in placements if ys <= p.filled_at < ye]

        points = []                       # [custody_start, lot]
        if carry:
            points.append([ys, carry.lot])
        for p in fills:
            points.append([p.filled_at, p.lot])
        if not carry and fills:
            points[0][0] = ys             # leading idle → first wine of the year
        points.sort(key=lambda x: x[0])

        if not points:
            overhead += slice_amt         # empty all year → idle-capacity overhead
            continue

        for i, (start, lot) in enumerate(points):
            end = points[i + 1][0] if i + 1 < len(points) else ye
            days = (end - start).days
            if days > 0:
                result[lot] += slice_amt * days / year_days

    return result, overhead


def lot_oak_depreciation(lot):
    """Total barrel depreciation cost this lot bears, across every barrel it occupied."""
    total = 0.0
    containers = {p.container for p in lot.placements.filter(voided_at__isnull=True)}
    for c in containers:
        alloc, _ = barrel_depreciation_by_lot(c)
        for l, amt in alloc.items():
            if l.id == lot.id:
                total += amt
    return round(total, 2)


# ============================================================ COGS rollup
def _estate_cost_per_ton():
    from cellar.models import ConfigConstant
    row = ConfigConstant.objects.filter(key="estate_fruit_cost_per_ton").first()
    try:
        return float(row.value) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _contract_price(lot, tag):
    """The vintage's contract price for this fruit, block-specific if there is one.

    Prices change every year, so they live in FruitPrice rows keyed on vintage —
    not in a single field that gets overwritten each harvest and silently restates
    the COGS of every prior vintage.
    """
    from cellar.models import FruitPrice
    block = getattr(getattr(tag, "harvest_event", None), "block", None)
    variety = getattr(block, "variety", None)
    if variety is None:
        from cellar.services import lotmeta
        variety = lotmeta.lot_variety(lot)
    if variety is None:
        return None
    return FruitPrice.for_lot(lot.vintage_year, variety, block)


def fruit_cost(lot):
    """Σ (allocated tons × cost/ton) over the lot's weigh-tag allocations.

    Resolution order: the cost recorded on the tag → the vintage's contract price
    (FruitPrice) → the purchase price on the tag → the estate constant.
    """
    total = 0.0
    for a in lot.allocations.filter(voided_at__isnull=True):
        tag = a.weigh_tag
        cpt = tag.fruit_cost_per_ton
        if cpt is None:
            cpt = _contract_price(lot, tag)
        if cpt is None:
            cpt = tag.purchase_price_per_ton if tag.source_type == "purchased" else _estate_cost_per_ton()
        tons = float(a.allocated_net_lbs) / 2000.0
        total += tons * float(cpt or 0)
    return total


def addition_cost(lot):
    return float(sum((a.cost for a in lot.additions.filter(voided_at__isnull=True)), 0))


def spirit_cost(lot):
    return float(sum((f.spirit_cost or 0 for f in lot.fortifications.filter(voided_at__isnull=True)), 0))


def lot_direct_cost(lot):
    """Costs incurred directly on this lot (not inherited from parents)."""
    return fruit_cost(lot) + addition_cost(lot) + spirit_cost(lot) + lot_oak_depreciation(lot)


def _lot_volume_for_cost(lot):
    from cellar.services.aging import _lot_volume
    return _lot_volume(lot)


def lot_cost(lot, _depth=0):
    """Total accumulated cost of a lot: its own direct costs plus cost inherited
    from every contributing parent lot (blends, splits, topping), at the parent's
    cost-per-gallon times the gallons contributed."""
    from cellar.models import LotLineage
    direct = lot_direct_cost(lot)
    inherited = 0.0
    if _depth <= 25:
        for edge in LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True).select_related("parent_lot"):
            pv = _lot_volume_for_cost(edge.parent_lot)
            if pv:
                inherited += (lot_cost(edge.parent_lot, _depth + 1) / pv) * float(edge.volume_gal or 0)
    return round(direct + inherited, 2)


def lot_cost_per_gal(lot):
    v = _lot_volume_for_cost(lot)
    return round(lot_cost(lot) / v, 4) if v else None


def bottling_cogs(run):
    """Full COGS for a bottling run and per-bottle / per-case unit cost.

    ROUNDING: dollar figures are rounded to the cent for presentation. Gallon inputs
    (run.bulk_gallons_in, volumes) are consumed at full precision — never pre-round a
    gallon figure that will be summed for a TTB report; round the summary at the report
    boundary to the nearest tenth gallon (27 CFR 24.281). See bottling.py.
    """
    cpg = lot_cost_per_gal(run.source_lot)
    bulk = float(run.bulk_gallons_in or 0)
    wine = round((cpg or 0) * bulk, 2)
    dry = float(sum((u.cost for u in run.dry_goods.filter(voided_at__isnull=True)), 0))
    line = float(run.line_labor_cost or 0)
    total = round(wine + dry + line, 2)
    bottles = run.bottles_produced or 1
    return {
        "wine_cost": wine, "dry_goods_cost": round(dry, 2), "line_labor_cost": round(line, 2),
        "total_cogs": total, "bottles": run.bottles_produced,
        "cost_per_bottle": round(total / bottles, 4),
        "cost_per_case": round(total / bottles * run.bottle_format.bottles_per_case, 2),
    }
