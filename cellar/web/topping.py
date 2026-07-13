"""
Topping and rack-out — front end (HTMX). Both live on the Oak tab, next to
rack-to-barrel, since all three operate on the same barrel/placement list.

Topping closes the loop the task rules opened (`rule_topping_interval` raises
"Top barrels" tasks) — completing the task before this wrote a TaskEvent and no
wine moved. `topping.top_barrels()` already books the evaporative loss and the
foreign-contribution lineage edge; this is just the form in front of it.

Rack-out is the deliberate empty-the-barrel(s) move. It clears the 5-gallon
foreign-wine flag (`AgingPlacement.is_flagged` reads `emptied_at`) and, if you
gauge what came out, trues up any accrued evaporation against the books.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import AgingPlacement, Lot, ToppingEvent
from cellar.services import topping as top_svc
from cellar.services import volumes as vol_svc


def _d(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def topping_source_lots(exclude_lot=None):
    """Lots that plausibly have wine on hand to top or blend from — everything
    past receiving/processing and not yet bottled. Sorted by code."""
    qs = (Lot.objects.select_related("current_designation")
          .exclude(status__in=[Lot.Status.PLANNED, Lot.Status.RECEIVING,
                                Lot.Status.BOTTLED]))
    if exclude_lot is not None:
        qs = qs.exclude(pk=exclude_lot.pk)
    rows = [l for l in qs if vol_svc.lot_balance(l)]
    rows.sort(key=lambda l: l.code)
    return rows


@login_required
@require_http_methods(["POST"])
def lot_top_barrels(request, pk):
    """Top one or more of THIS lot's own barrels, from a chosen source lot
    (usually itself — 'top from the same wine' — but can be a different lot,
    which is the foreign-contribution case the 5-gallon flag watches for)."""
    from . import views  # local import: avoids a circular at module load
    lot = get_object_or_404(Lot, pk=pk)
    error = None
    try:
        source_pk = request.POST.get("source_lot")
        source_lot = Lot.objects.get(pk=source_pk) if source_pk else lot
        placement_pks = request.POST.getlist("placements")
        if not placement_pks:
            raise ValueError("Select at least one barrel to top.")
        per_barrel = {}
        for ppk in placement_pks:
            raw = (request.POST.get(f"gal_{ppk}") or "").strip()
            if raw:
                per_barrel[int(ppk)] = raw
        total_gal = request.POST.get("total_gal") or None
        kind = (request.POST.get("kind") or ToppingEvent.Kind.ROUTINE)

        top_svc.top_barrels(
            source_lot,
            topped_at=parse_date(request.POST.get("topped_at") or "") or timezone.localdate(),
            placements=[int(p) for p in placement_pks],
            total_gal=(None if per_barrel else total_gal),
            per_barrel=(per_barrel or None),
            kind=kind,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        error = str(e)
    return views.render_oak_panel(request, lot, error=error)


@login_required
@require_http_methods(["POST"])
def lot_rack_out(request, pk):
    """Empty barrels back to a vessel — clears the foreign-wine flag on each,
    and trues up accrued evaporation if a gauge is given."""
    from . import views  # local import: avoids a circular at module load
    from cellar.models import Vessel
    lot = get_object_or_404(Lot, pk=pk)
    error = None
    try:
        placement_pks = request.POST.getlist("placements")
        if not placement_pks:
            raise ValueError("Select at least one barrel to rack out.")
        to_vessel_pk = request.POST.get("to_vessel") or None
        to_vessel = Vessel.objects.get(pk=to_vessel_pk) if to_vessel_pk else None

        top_svc.rack_out(
            [int(p) for p in placement_pks],
            racked_at=parse_date(request.POST.get("racked_at") or "") or timezone.localdate(),
            to_vessel=to_vessel,
            gauged_gal=request.POST.get("gauged_gal") or None,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        error = str(e)
    return views.render_oak_panel(request, lot, error=error)
