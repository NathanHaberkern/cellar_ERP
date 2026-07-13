"""
Book-to-bond — front end (HTMX). Lives on the lot summary, not behind a tab.

Booking is the act that ends primary, so it belongs on the landing card where the
cellar already looks for the lot's state — not buried as "Fermentation Step 5". The
card appears once the wine is off the skins and disappears the moment it is booked,
replaced by the booking receipt.

The gallons field carries a gauge picker: tank gauge, barrel fill, or stated. The
default is chosen by `bonding.gauge_options()` — barrel fill when the lot is fully
down (no sensor: the barrel-down IS the gauge), tank gauge otherwise.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Container, Lot, TaxClass
from cellar.services import barreling as bar
from cellar.services import bonding as bond


def bond_ctx(lot, error=None, ok=None):
    gauges = bond.gauge_options(lot)
    return {
        "lot": lot,
        "can_book": bond.can_book_to_bond(lot),
        "in_bond": bond.is_in_bond(lot),
        "booking": bond.booking_for(lot),
        "gauges": gauges,
        "tax_classes": TaxClass.choices,
        "default_tax_class": bond.default_tax_class(lot),
        "today": timezone.localdate().isoformat(),
        "error": error,
        "ok": ok,
    }


@login_required
def lot_bond_card(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_bond_card.html", bond_ctx(lot))


@login_required
@require_http_methods(["POST"])
def lot_book_to_bond(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        bond.book_to_bond(
            lot,
            gallons_produced=request.POST.get("gallons"),
            gauge_source=(request.POST.get("gauge_source") or bond.GaugeSource.STATED),
            booked_at=parse_date(request.POST.get("booked_at") or "") or timezone.localdate(),
            tax_class=request.POST.get("tax_class") or None,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_bond_card.html", bond_ctx(lot, error=str(e)))
    # status flipped to done_primary — the tab bar changes, so refresh the page
    resp = render(request, "web/_lot_bond_card.html",
                  bond_ctx(lot, ok=f"{lot.code} booked to bond."))
    resp["HX-Refresh"] = "true"
    return resp


# ------------------------------------------------------------------- barreling
@login_required
def oak_barrel_search(request, pk):
    """HTMX GET — filtered/scanned empty-barrel lookup for the barrel-down picker.

    Deliberately never returns an unbounded list (see
    `barreling.search_empty_oak_containers`): the front end always sends at
    least a text query, type, or format filter. If `q` is an exact ID/barcode
    match (the scanner-wedge case — type or scan a code, hit Enter), we signal
    that back so the client can auto-add without a click, matching the
    scan-to-move flow's existing "resolve then act in one step" feel.
    """
    lot = get_object_or_404(Lot, pk=pk)
    q = request.GET.get("q") or ""
    ctype = request.GET.get("type") or ""
    fmt = request.GET.get("fmt") or ""
    result = bar.search_empty_oak_containers(q=q, type=ctype, fmt=fmt)
    exact = bar.find_empty_oak_container(q) if q else None
    return render(request, "web/_barrel_search_results.html", {
        "lot": lot, "q": q, "result": result, "exact": exact,
    })


@login_required
@require_http_methods(["POST"])
def lot_rack_to_barrel(request, pk):
    """Barrel-down with per-barrel actuals. Does NOT change lot status."""
    from . import views  # local import: avoids a circular at module load
    lot = get_object_or_404(Lot, pk=pk)
    error = None
    try:
        fills = bar.parse_fills(request.POST)
        bar.rack_to_barrel(
            lot,
            fills=fills,
            filled_at=parse_date(request.POST.get("filled_at") or "") or timezone.localdate(),
            tank_disposition=(request.POST.get("tank_disposition")
                              or bar.TankDisposition.REMAINS),
            lees_gal=request.POST.get("lees_gal") or None,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        error = str(e)
    return views.render_oak_panel(request, lot, error=error)
