"""
Oak tile v2 — the column → rack → barrel representation and the two-phase,
pool-aware rack-down flow.

Display: a lot's barrels grouped by column → rack → position, plus oak-tier
percentages (SS drums excluded — they aren't oak). Never a spatial grid, never
the whole fleet: always scoped to this lot's ~dozens of barrels.

Fill flow: operate on RACKS, not loose barrels.
  * Phase 1 (pull) — the picker lists EMPTY racks in the lot's pool grouped by
    their current column, so the sheet tells the cellar where to pull from.
  * Phase 2 (record) — the chosen barrels are filled (per-barrel volume defaults
    to that barrel's capacity − headspace, so mixed 55/60/70/130 gauge right) and
    the racks move to the END column. Reuses the proven parse_fills +
    barreling.rack_to_barrel commit path.
"""
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from cellar.models import Container, Location, RackAssignment
from cellar.models.aging import OakTier
from cellar.services import barreling, lotmeta
from cellar.services import bonding as bond_svc

HEADSPACE = Decimal("3")


def _pool_for(lot):
    return Container.Pool.PORT if lotmeta.is_port(lot) else Container.Pool.TABLE


def _fill_default(container):
    cap = container.capacity_gal or Decimal("0")
    return (cap - HEADSPACE) if cap > HEADSPACE else cap


# ---- display --------------------------------------------------------------
_TIER_ORDER = [OakTier.NEW, OakTier.FIRST, OakTier.SECOND, OakTier.NEUTRAL]
_TIER_LABEL = {OakTier.NEW: "New", OakTier.FIRST: "1st use",
               OakTier.SECOND: "2nd use", OakTier.NEUTRAL: "Neutral"}


def display_ctx(lot):
    placements = list(
        lot.placements.filter(emptied_at__isnull=True, voided_at__isnull=True)
        .select_related("container"))

    # tier breakdown across OAK barrels only (SS drums / tanks excluded)
    oak = [p for p in placements if p.container.is_oak]
    tier_counts = defaultdict(int)
    for p in oak:
        tier_counts[p.oak_tier or OakTier.NEUTRAL] += 1
    n_oak = len(oak) or 1
    tiers = [{"key": t, "label": _TIER_LABEL[t], "count": tier_counts.get(t, 0),
              "pct": round(tier_counts.get(t, 0) / n_oak * 100)} for t in _TIER_ORDER]

    # group by column (location) → rack → positions
    by_loc = defaultdict(lambda: defaultdict(list))
    unracked = []
    for p in placements:
        c = p.container
        ra = c.current_rack_assignment()
        if ra and ra.rack:
            loc = ra.rack.location.code if ra.rack.location_id else "(no column)"
            by_loc[loc][ra.rack.rack_id].append(
                {"barrel": c.container_id, "size": c.capacity_gal, "pos": ra.position,
                 "tier": _TIER_LABEL.get(p.oak_tier, p.oak_tier or "—"),
                 "volume": p.volume_gal, "is_oak": c.is_oak})
        else:
            unracked.append({"barrel": c.container_id, "size": c.capacity_gal,
                             "volume": p.volume_gal})

    columns = []
    for loc in sorted(by_loc):
        racks = []
        for rid in sorted(by_loc[loc]):
            barrels = sorted(by_loc[loc][rid], key=lambda b: b["pos"])
            racks.append({"rack_id": rid, "barrels": barrels})
        columns.append({"column": loc, "racks": racks,
                        "barrel_count": sum(len(r["barrels"]) for r in racks)})

    return {
        "tiers": tiers,
        "barrel_count": len(placements),
        "oak_count": len(oak),
        "total_gallons": bond_svc.barrel_fill_total(lot),
        "columns": columns,
        "unracked": unracked,
    }


# ---- fill flow ------------------------------------------------------------
def fill_ctx(lot):
    """Phase 1 pull sheet: empty racks in the lot's pool, grouped by column."""
    pool = _pool_for(lot)
    empties = (barreling.empty_oak_qs().filter(pool=pool)
               .order_by("container_id"))
    # attach each empty barrel to its rack + column via the open rack assignment
    assigns = {a.container_id: a for a in
               RackAssignment.objects.filter(container__in=empties, removed_at__isnull=True)
               .select_related("rack", "rack__location")}

    by_col = defaultdict(lambda: defaultdict(list))
    loose = []
    for c in empties:
        a = assigns.get(c.id)
        if a and a.rack:
            col = a.rack.location.code if a.rack.location_id else "(no column)"
            by_col[col][a.rack.rack_id].append(
                {"pk": c.id, "barrel": c.container_id, "size": c.capacity_gal,
                 "pos": a.position, "default": _fill_default(c)})
        else:
            loose.append({"pk": c.id, "barrel": c.container_id,
                          "size": c.capacity_gal, "default": _fill_default(c)})

    columns = []
    total_empty = 0
    for col in sorted(by_col):
        racks = []
        for rid in sorted(by_col[col]):
            bs = sorted(by_col[col][rid], key=lambda b: b["pos"])
            racks.append({"rack_id": rid, "barrels": bs})
            total_empty += len(bs)
        columns.append({"column": col, "racks": racks,
                        "empty_barrels": sum(len(r["barrels"]) for r in racks)})
    total_empty += len(loose)

    return {
        "pool": pool, "pool_label": lot and _pool_for(lot).label,
        "columns": columns, "loose": loose,
        "total_empty": total_empty,
        "end_locations": Location.objects.select_related("room").order_by("code"),
    }


@login_required
def oak_barrels(request, pk):
    from cellar.models.spine import Lot
    lot = get_object_or_404(Lot, pk=pk)
    ctx = display_ctx(lot)
    ctx["lot"] = lot
    return render(request, "web/_oak_barrels.html", ctx)


@login_required
def oak_fill(request, pk):
    from cellar.models.spine import Lot
    lot = get_object_or_404(Lot, pk=pk)
    ctx = fill_ctx(lot)
    ctx["lot"] = lot
    ctx["today"] = timezone.localdate().isoformat()
    return render(request, "web/_oak_fill.html", ctx)


@login_required
def oak_fill_commit(request, pk):
    from cellar.models.spine import Lot
    lot = get_object_or_404(Lot, pk=pk)
    if request.method != "POST":
        return oak_fill(request, pk)

    try:
        fills = barreling.parse_fills(request.POST)
        if not fills:
            raise ValueError("Select at least one barrel to fill.")
        filled_at = request.POST.get("filled_at") or None
        barreling.rack_to_barrel(lot, fills=fills, filled_at=filled_at,
                                 actor=getattr(request, "user", None))

        # move the racks of the filled barrels to the END column (if chosen)
        end_code = (request.POST.get("end_column") or "").strip()
        moved = 0
        if end_code and end_code != "__keep__":
            end_loc = Location.objects.filter(code=end_code).first()
            if end_loc:
                container_ids = [f["container"].id for f in fills]
                rack_ids = set(
                    RackAssignment.objects
                    .filter(container_id__in=container_ids, removed_at__isnull=True)
                    .values_list("rack_id", flat=True))
                for rack in end_loc.racks.model.objects.filter(id__in=rack_ids):
                    if rack.location_id != end_loc.id:
                        rack.location = end_loc
                        rack.save(update_fields=["location"])
                        moved += 1
    except Exception as exc:  # noqa: BLE001 - surface the message inline
        ctx = fill_ctx(lot)
        ctx["lot"] = lot
        ctx["error"] = str(exc)
        return render(request, "web/_oak_fill.html", ctx)

    # success → show the updated barrels view
    return oak_barrels(request, pk)
