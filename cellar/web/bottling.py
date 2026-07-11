"""
Bottling module — front end (HTMX), lives on the lot page as its own tab.

The panel is the same URL for both sides of the split and switches on what the lot
IS, so the cellar never has to think about which screen they want:

  * a finished BULK lot  -> "Prepare for bottling": rack N gallons into a parcel
                            (25VERD_B1), plus a list of parcels already taken off it
  * a BOTTLING PARCEL    -> "Bottle": SKU, format, cases -> a BottlingRun, plus the
                            runs already recorded and the case counts

The tab only appears on wine that's finished primary (or already a parcel), so it
can't be reached mid-ferment.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Lot, BottleFormat, Vessel
from cellar.services import bottling as bz
from cellar.services import operations as ops

from . import lotpages
from .vessels import vessel_options


def _parse_dt(raw):
    from django.utils.dateparse import parse_datetime
    raw = (raw or "").strip()
    if not raw:
        return timezone.now()
    dt = parse_datetime(raw)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt or timezone.now()


def bottling_ctx(lot, error=None):
    parcel = bz.is_parcel(lot)
    ctx = {
        "lot": lot,
        "section": "bottling",
        "is_parcel": parcel,
        "can_split": bz.can_split(lot),
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "today": timezone.localdate().isoformat(),
        "volume": ops.current_volume(lot),
        "error": error,
        "note": lotpages.section_note(lot, "bottling"),
    }
    if parcel:
        ctx.update({
            "parent": bz.parent_of(lot),
            "formats": BottleFormat.objects.order_by("ml"),
            "runs": bz.runs_for(lot),
            "next_suffix": None,
        })
    else:
        ctx.update({
            "parcels": bz.parcels_of(lot),
            "vessel_options": vessel_options(exclude_lot=lot),
        })
    return ctx


@login_required
def lot_bottling(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_bottling.html", bottling_ctx(lot))


@login_required
@require_http_methods(["POST"])
def bottling_prepare(request, pk):
    """Rack a parcel off a finished bulk lot."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        vessel_pk = (request.POST.get("vessel") or "").strip()
        vessel = Vessel.objects.filter(pk=vessel_pk).first() if vessel_pk else None
        bz.create_parcel(
            lot, volume_gal=request.POST.get("volume"),
            vessel=vessel,
            at=_parse_dt(request.POST.get("racked_at")),
            allow_blend=request.POST.get("allow_blend") == "on",
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_bottling.html", bottling_ctx(lot, error=str(e)))
    return render(request, "web/_lot_bottling.html", bottling_ctx(lot))


@login_required
@require_http_methods(["POST"])
def bottling_run(request, pk):
    """Bottle a parcel into a SKU."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        bz.bottle_parcel(
            lot,
            sku=(request.POST.get("sku") or "").strip(),
            bottle_format=request.POST.get("bottle_format"),
            cases_produced=request.POST.get("cases") or 0,
            bottled_at=parse_date(request.POST.get("bottled_at") or "") or timezone.localdate(),
            bulk_gallons_in=request.POST.get("gallons_in") or None,
            line_labor_cost=request.POST.get("labor") or 0,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_bottling.html", bottling_ctx(lot, error=str(e)))
    resp = render(request, "web/_lot_bottling.html", bottling_ctx(lot))
    resp["HX-Refresh"] = "true"        # status flipped to bottled — refresh the tabs
    return resp
