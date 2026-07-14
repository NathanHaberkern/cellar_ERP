"""
Lot detail page — read models.

The individual-lot page is a summary landing card plus six data sub-pages
(Additions, Labs, Movement, Composition, Oak, Cost) and a Tasks placeholder.
Everything here READS the same append-only ledger the filings are built from and
shapes it for the templates; nothing here writes. The write paths (record an
addition, add a lab result, book a transfer, save a section note) live in
views.py and call cellar/services/ directly, same as the rest of the web layer.

Design decisions baked in here:
  * disposition — a lot is "In bond" once it has a BookToBond (straight ferment)
    OR a FortificationEvent (Port). In this data model fortification IS the
    production-to-bond booking for Port lots — there's no separate book-to-bond
    row for them — so keying only on BookToBond would leave every Port lot stuck
    reading "In fermenter". Both events are checked. (One function; trivially
    narrowed to BookToBond-only if that's ever wanted.)
  * ferment glance — latest Brix / Temp show ONLY while the lot is in the
    fermenter, and disappear once it's booked to bond.
  * Movement — derived, not a new ledger. Each row is projected from the event
    that actually recorded it (TankAssignment, LotLineage, VolumeLoss / topping,
    BottlingRun, AgingPlacement, BulkTaxPaidRemoval, BondTransfer), so topping
    loss / gain rows are exactly the ones 5120.17 reads.
"""
from collections import defaultdict

from django.utils import timezone

from cellar.models import (
    Lot, TankAssignment, Reading, VolumeMeasurement, LotLineage, VolumeLoss,
    BottlingRun, AgingPlacement, BulkTaxPaidRemoval, BondTransfer, LotSectionNote,
    Vessel, WeighTagAllocation,
)

# Bin-type vessels crushed together (same lot, same instant) collapse to one
# summary movement row instead of one "Racking" row per bin — see movements().
_BIN_TYPES = (Vessel.Type.MACRO_BIN, Vessel.Type.ONE_TON_BIN)
from cellar.services import aging as aging_svc
from cellar.services import operations as ops
from cellar.services import labpanels
from cellar.services import volumes as vol_svc


# --------------------------------------------------------------- disposition
def is_in_bond(lot):
    """Delegates to the service — this is domain logic, not a view concern."""
    from cellar.services import bonding
    return bonding.is_in_bond(lot)


def disposition(lot):
    """Three real states, not two. 'Not yet booked' is the honest label for wine
    that is off the skins but whose production has not been declared — the old
    binary called it 'In fermenter', which was flatly wrong for a pressed lot
    sitting in tank waiting to be gauged."""
    from cellar.services import bonding
    if bonding.is_in_bond(lot):
        return "In bond"
    if lot.status in (Lot.Status.PRESSED, Lot.Status.SETTLING):
        return "Not yet booked"
    return "In fermenter"


# ------------------------------------------------------------------ location
def current_location(lot):
    """Tank code(s) and/or barrel count for wherever the lot currently sits."""
    tanks = (TankAssignment.objects
             .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
             .select_related("vessel").order_by("vessel__code"))
    parts = [a.vessel.code for a in tanks]

    placements = (lot.placements.filter(emptied_at__isnull=True, voided_at__isnull=True)
                  .select_related("container"))
    barrels = [p for p in placements if p.container.is_oak]
    parts += [p.container.container_id for p in placements if not p.container.is_oak]
    if barrels:
        parts.append(f"{len(barrels)} barrel{'s' if len(barrels) != 1 else ''}")
    return ", ".join(parts) if parts else "—"


def current_gallons(lot):
    v = ops.current_volume(lot)
    if v is not None:
        return v
    vm = VolumeMeasurement.booking_volume_for(lot)
    return vm.volume_gal if vm else None


def latest_readings(lot):
    """Latest Brix / Temp — only while fermenting; None once booked to bond."""
    if is_in_bond(lot):
        return None
    out = {}
    for analyte, label in ((Reading.Analyte.BRIX, "Brix"), (Reading.Analyte.TEMP, "Temp °F")):
        r = (Reading.objects.filter(lot=lot, analyte=analyte, voided_at__isnull=True)
             .order_by("-measured_at", "-id").first())
        if r:
            out[label] = {"value": r.value, "at": r.measured_at}
    return out or None


def _value_view(v):
    """Shape one LabResultValue for display — the label the report shows, plus unit/flag."""
    return {
        "analyte": v.analyte.name,
        "display": v.display or (f"{v.value:g}" if v.value is not None else ""),
        "unit": v.analyte.unit,
        "flag": v.flag,
    }


def latest_panel(lot):
    """The most recent *full* juice/chemistry panel, pinned to the summary card,
    with a count of newer partial results. None if the lot has no full panel yet."""
    result, newer = labpanels.latest_full_panel(lot)
    if result is None:
        return None
    values = sorted(result.values.all(), key=lambda v: v.analyte.sort_order)
    return {
        "panel": result.get_panel_display(),
        "date": result.reported_at,
        "source": result.get_source_display(),
        "sample_id": result.sample_id,
        "values": [_value_view(v) for v in values],
        "newer_partials": newer,
    }


def summary(lot):
    return {
        "location": current_location(lot),
        "gallons": current_gallons(lot),
        "disposition": disposition(lot),
        "in_bond": is_in_bond(lot),
        "readings": latest_readings(lot),
        "panel": latest_panel(lot),
    }


# ----------------------------------------------------- fermentation progress
_SPARK_W, _SPARK_H, _SPARK_PAD = 260, 56, 6


def _sparkline_points(series):
    """[(date, value), ...] -> SVG polyline 'points' string, normalized into a
    fixed-size box. Returns None for <2 points (nothing to draw a line between).
    Computed here rather than in the template — Django templates can't do the
    min/max scaling arithmetic this needs.
    """
    if len(series) < 2:
        return None
    values = [v for _, v in series]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(series)
    xs = [_SPARK_PAD + i * (_SPARK_W - 2 * _SPARK_PAD) / (n - 1) for i in range(n)]
    ys = [_SPARK_PAD + (1 - (v - lo) / span) * (_SPARK_H - 2 * _SPARK_PAD) for v in values]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def ferment_progress(lot):
    """Sugar-depletion sparkline + estimated press/barrel-down dates for the
    lot summary card. Only meaningful pre-bond (see is_in_bond) — the caller
    gates display on that, same as the existing 'Ferment (latest)' card.
    """
    from cellar.services import fermentation as ferm_svc
    series = ferm_svc.brix_series(lot)
    estimate = ferm_svc.estimate_press_and_barrel_dates(lot)
    return {
        "brix_series": series,
        "spark_points": _sparkline_points(series),
        "spark_w": _SPARK_W, "spark_h": _SPARK_H,
        "latest_brix": series[-1][1] if series else None,
        "estimate": estimate,
    }


# ------------------------------------------------------------------ timeline
def timeline(lot, limit=25):
    """Merged, read-only event history for the lot summary card: additions,
    Brix/temp readings, and movements in one chronological list. Each source
    already has its own full tab (Additions / Movement / etc.) — this exists
    so the first thing you see after clicking a tank is 'what has actually
    happened here', without clicking into each tab separately.

    Capped at `limit` (most recent first) — this is a glance, not the ledger;
    the individual tabs remain the source of the complete, uncapped history.
    """
    rows = []

    for a in additions(lot):
        rows.append({
            "date": _d(a["date"]), "kind": "Addition",
            "label": a["addition"],
            "detail": (a["qty"] or "") + (f" · target {a['rate']}" if a["rate"] else ""),
        })

    for r in (Reading.objects.filter(lot=lot, voided_at__isnull=True)
              .order_by("-measured_at")[:200]):
        unit = "°Brix" if r.analyte == Reading.Analyte.BRIX else "°F"
        rows.append({
            "date": _d(r.measured_at), "kind": "Reading",
            "label": f"{r.value} {unit}",
            "detail": r.get_analyte_display(),
        })

    for m in movements(lot):
        rows.append({
            "date": m["date"], "kind": m["type"],
            "label": f"{m['start']} → {m['end']}",
            "detail": (f"{m['gallons']} gal" if m["gallons"] is not None else "") +
                      (f" · {m['note']}" if m["note"] else ""),
        })

    rows.sort(key=lambda r: (r["date"] is None, r["date"]), reverse=True)
    return rows[:limit]


# ----------------------------------------------------------------- additions
def additions(lot):
    rows = []
    for a in (lot.additions.filter(voided_at__isnull=True)
              .select_related("additive").order_by("-added_at", "-id")):
        if a.computed_dose:
            qty = a.computed_dose
        elif a.quantity is not None:
            qty = f"{a.quantity:g} {a.additive.unit}".strip()
        else:
            qty = ""
        rows.append({
            "date": a.added_at,
            "addition": a.additive.name,
            "rate": a.target,
            "qty": qty,
            "note": a.notes,
        })
    return rows


# ---------------------------------------------------------------------- labs
def labs(lot):
    """Every result as a panel card, newest first — each labelled with its panel
    type (Juice / Chemistry / …) and whether it's a full panel. Analyte values
    carry the display label + flag so censored / qualitative readings show as
    ND / Dry / FAIL rather than a bare zero."""
    results = (lot.lab_results.filter(voided_at__isnull=True)
               .prefetch_related("values__analyte").order_by("-reported_at", "-id"))
    cards = []
    for r in results:
        values = sorted(r.values.all(), key=lambda v: v.analyte.sort_order)
        cards.append({
            "sample_id": r.sample_id or "—",
            "panel": r.get_panel_display(),
            "panel_key": r.panel,
            "is_full": labpanels.result_is_full(r),
            "date": r.reported_at,
            "source": r.get_source_display(),
            "note": r.notes,
            "values": [_value_view(v) for v in values],
        })
    return cards


# ------------------------------------------------------------------ movement
def _d(value):
    """Normalize a date / datetime to a date for uniform sorting + display.

    NB: datetime is a SUBCLASS of date, so `isinstance(dt, date)` is True for both.
    Any guard phrased as "is it not a date?" therefore never fires on a datetime and
    lets it through unconverted — which blows up the sort below the moment one lot
    carries both a datetime-sourced row (tank assignment) and a date-sourced one
    (bottling run, aging placement) with "can't compare datetime to date". Test for
    datetime FIRST.
    """
    from datetime import datetime as _datetime
    if isinstance(value, _datetime):
        try:
            return timezone.localtime(value).date()
        except (ValueError, TypeError):
            return value.date()
    return value


def movements(lot):
    """Unified, read-only movement timeline projected from the ledger.

    Types mirror the cellar's vocabulary: Racking (tank assignment), Blending,
    Topping gain / Topping loss, Bottling, Barrel down, Sale, Bond transfer.
    """
    rows = []

    # Racking / tank moves — each assignment; 'from' = the lot's prior vessel.
    # Bin-type assignments made together at crush (same instant, several macro/
    # 1-ton bins) collapse to one summary row — a 14-bin crush shouldn't read as
    # 14 separate "Racking" lines. Anything else (single bin, or a later rack
    # move onto a real tank) keeps the normal per-assignment row.
    assigns = list(TankAssignment.objects.filter(lot=lot, voided_at__isnull=True)
                   .select_related("vessel").order_by("assigned_at", "id"))
    prev_vessel = None
    i = 0
    while i < len(assigns):
        a = assigns[i]
        if a.vessel.type in _BIN_TYPES:
            group = [a]
            j = i + 1
            while (j < len(assigns) and assigns[j].vessel.type in _BIN_TYPES
                   and assigns[j].assigned_at == a.assigned_at):
                group.append(assigns[j])
                j += 1
            if len(group) > 1:
                tags = (WeighTagAllocation.objects.filter(lot=lot, voided_at__isnull=True)
                        .select_related("weigh_tag").order_by("id"))
                tag_label = ", ".join(dict.fromkeys(
                    t.weigh_tag.weigh_tag_number for t in tags)) or "fruit"
                vm = VolumeMeasurement.objects.filter(
                    lot=lot, voided_at__isnull=True, measured_at=a.assigned_at).first()
                gallons = vm.volume_gal if vm else sum(
                    (g.vessel.capacity_gal or 0) for g in group)
                rows.append({
                    "type": "Crush", "date": _d(a.assigned_at),
                    "start": prev_vessel or "—", "end": f"{len(group)} macro bins",
                    "gallons": gallons,
                    "note": f"Crush {tag_label} \u2192 {len(group)} macro bins"})
                prev_vessel = None  # ambiguous which single bin is "current" after a group
            else:
                rows.append({
                    "type": "Racking", "date": _d(a.assigned_at),
                    "start": prev_vessel or "—", "end": a.vessel.code,
                    "gallons": None, "note": a.notes or ""})
                prev_vessel = a.vessel.code
            i = j
        else:
            rows.append({
                "type": "Racking", "date": _d(a.assigned_at),
                "start": prev_vessel or "—", "end": a.vessel.code,
                "gallons": None, "note": a.notes or ""})
            prev_vessel = a.vessel.code
            i += 1

    # Blending (lot is child = received blend; lot is parent = blended out)
    blend_rels = {LotLineage.Relationship.WHOLE_BLEND, LotLineage.Relationship.PARTIAL_BLEND}
    for e in (LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True,
                                        relationship_type__in=blend_rels)
              .select_related("parent_lot")):
        rows.append({"type": "Blending", "date": _d(e.created_at),
                     "start": e.parent_lot.code, "end": lot.code,
                     "gallons": e.volume_gal, "note": e.get_relationship_type_display()})
    for e in (LotLineage.objects.filter(parent_lot=lot, voided_at__isnull=True,
                                        relationship_type__in=blend_rels)
              .select_related("child_lot")):
        rows.append({"type": "Blending", "date": _d(e.created_at),
                     "start": lot.code, "end": e.child_lot.code,
                     "gallons": e.volume_gal, "note": e.get_relationship_type_display()})

    # Bottling parcel split — both directions, so the bulk lot shows what left and the
    # parcel shows where it came from.
    for e in (LotLineage.objects.filter(parent_lot=lot, voided_at__isnull=True,
                                        relationship_type=LotLineage.Relationship.BOTTLING_SPLIT)
              .select_related("child_lot")):
        rows.append({"type": "Bottling prep", "date": _d(e.created_at),
                     "start": lot.code, "end": e.child_lot.code,
                     "gallons": -e.volume_gal if e.volume_gal is not None else None,
                     "note": "racked off for bottling"})
    for e in (LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True,
                                        relationship_type=LotLineage.Relationship.BOTTLING_SPLIT)
              .select_related("parent_lot")):
        rows.append({"type": "Bottling prep", "date": _d(e.created_at),
                     "start": e.parent_lot.code, "end": lot.code,
                     "gallons": e.volume_gal, "note": "parcel racked off bulk"})

    # Topping gain — foreign wine topped into this lot's barrels (LotLineage TOPPING)
    for e in (LotLineage.objects.filter(child_lot=lot, voided_at__isnull=True,
                                        relationship_type=LotLineage.Relationship.TOPPING)
              .select_related("parent_lot")):
        rows.append({"type": "Topping gain", "date": _d(e.created_at),
                     "start": e.parent_lot.code, "end": lot.code,
                     "gallons": e.volume_gal, "note": "topping contribution"})

    # Topping loss — evaporative loss booked on this lot (auto from ToppingTarget),
    # plus any other recorded volume loss. Kept as the exact rows 5120.17 reads.
    for v in VolumeLoss.objects.filter(lot=lot, voided_at__isnull=True):
        is_top = "topping" in (v.reason or "").lower()
        rows.append({"type": "Topping loss" if is_top else "Loss",
                     "date": _d(v.occurred_at), "start": lot.code, "end": "—",
                     "gallons": -v.volume_gal if v.volume_gal is not None else None,
                     "note": v.reason})

    # Bottling
    for b in lot.bottlings.filter(voided_at__isnull=True).select_related("bottle_format"):
        rows.append({"type": "Bottling", "date": _d(b.bottled_at),
                     "start": lot.code, "end": f"bottled · {b.sku}",
                     "gallons": b.bulk_gallons_in, "note": f"{b.cases_produced} cs"})

    # Barrel down — oak fills grouped by (date, format); other-container fills listed
    oak_groups = defaultdict(lambda: {"count": 0, "gallons": 0})
    for p in (lot.placements.filter(voided_at__isnull=True)
              .select_related("container").order_by("filled_at")):
        c = p.container
        if c.is_oak:
            key = (p.filled_at, c.format or c.get_type_display())
            oak_groups[key]["count"] += 1
            oak_groups[key]["gallons"] += float(p.volume_gal or 0)
        else:
            rows.append({"type": "Barrel down", "date": _d(p.filled_at),
                         "start": lot.code, "end": c.container_id,
                         "gallons": p.volume_gal, "note": c.get_type_display()})
    for (filled_at, fmt), agg in oak_groups.items():
        rows.append({"type": "Barrel down", "date": _d(filled_at), "start": lot.code,
                     "end": f"{agg['count']} × {fmt}", "gallons": round(agg["gallons"], 1),
                     "note": "to barrel"})

    # Sale — bulk taxpaid removal
    for r in lot.bulk_removals.filter(voided_at__isnull=True):
        rows.append({"type": "Sale", "date": _d(r.removed_at), "start": lot.code,
                     "end": r.get_channel_display(),
                     "gallons": -r.wine_gallons if r.wine_gallons is not None else None,
                     "note": "bulk taxpaid removal"})

    # Sale — bulk must/juice, pre-fermentation (never a TTB wine-gallon event)
    for s in lot.must_sales.filter(voided_at__isnull=True):
        rows.append({"type": "Sale", "date": _d(s.sold_at), "start": lot.code,
                     "end": s.destination.name if s.destination else "—",
                     "gallons": -s.gallons if s.gallons is not None else None,
                     "note": s.notes or "must/juice sale"})

    # Bond transfer to / from another bonded premises
    for t in BondTransfer.objects.filter(lot=lot, voided_at__isnull=True):
        out = t.direction == BondTransfer.Direction.OUT
        rows.append({"type": "Bond transfer", "date": _d(t.transferred_at),
                     "start": lot.code if out else (t.counterparty or "in bond"),
                     "end": (t.counterparty or "out") if out else lot.code,
                     "gallons": (-t.gallons if out else t.gallons),
                     "note": t.get_direction_display()})

    rows.sort(key=lambda r: (r["date"] is None, r["date"]), reverse=True)
    return rows


# --------------------------------------------------------------- composition
def composition(lot):
    """Computed leaf-lot composition (read-only — derived from genealogy)."""
    return aging_svc.composition_report(lot)


# ---------------------------------------------------------------------- oak
_TIER_ORDER = ["New", "1st use", "2nd use", "Neutral"]


def oak(lot):
    """% by oak tier, current barrels + sizes, and current racks + locations."""
    raw = aging_svc.oak_summary(lot)                 # {tier_display: pct}
    tiers = {t: raw.get(t, 0.0) for t in _TIER_ORDER}

    placements = (lot.placements.filter(emptied_at__isnull=True, voided_at__isnull=True)
                  .select_related("container"))
    barrels, total_gal = [], 0.0
    for p in placements:
        c = p.container
        if not c.is_oak:
            continue
        cur_vol = float(vol_svc.placement_volume(p))
        total_gal += cur_vol
        capacity = float(c.capacity_gal) if c.capacity_gal else None
        ullage = round(capacity - cur_vol, 1) if capacity is not None else None
        barrels.append({
            "placement_pk": p.pk,
            "container_id": c.container_id,
            "size": c.format or (f"{c.capacity_gal:g} gal" if c.capacity_gal else c.get_type_display()),
            "tier": p.get_oak_tier_display(),
            "location": (c.effective_location().code if c.effective_location() else "—"),
            "current_gal": round(cur_vol, 1),
            "capacity_gal": capacity,
            # ullage = gap to full; used to default a routine topping amount so the
            # operator isn't stuck typing a number for every barrel — see _lot_oak.html
            "ullage_gal": ullage if (ullage is not None and ullage > 0) else None,
            "is_flagged": p.is_flagged,
        })

    racks = []
    for r in aging_svc.racks_holding_lot(lot):
        loc = r.location
        racks.append({
            "rack_id": r.rack_id,
            "location": (f"{loc.room.name}, {loc.code}" if loc else "—"),
        })

    return {
        "tiers": tiers,
        "barrel_count": len(barrels),
        "total_oak_gallons": round(total_gal, 1),
        "barrels": barrels,
        "racks": racks,
    }


# --------------------------------------------------------------------- notes
def section_note(lot, section):
    row = LotSectionNote.objects.filter(lot=lot, section=section).first()
    return row.body if row else ""


def save_section_note(lot, section, body, user=None):
    row, _ = LotSectionNote.objects.get_or_create(lot=lot, section=section)
    row.body = body or ""
    row.updated_by = user if (user and user.is_authenticated) else None
    row.save()
    return row
