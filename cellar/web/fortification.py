"""
Fortification / Port — front end (HTMX).

Shows only on lots designated to the Port program (`lotmeta.is_port`); the tab
itself is conditionally rendered in lot_detail.html the same way the
Fermentation and Bottling tabs already are (`show_fortification`).

Two forms live here, matching the two entry points in services/fortification.py:

  INITIAL     — Port fortified on skins. Only offered before the lot has any
                fortification or book-to-bond (a fortification IS the booking
                for a Port lot — see fortification.fortify()'s guard).
  ADJUSTMENT  — spring racking alcohol top-up. Only offered once the lot is
                already fortified (there is a base to adjust).

Both forms use fortification.pg_required() for a live "how much spirit do I
need" preview before committing, driven by the same HPGS blended proof the
service will actually draw from.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import HighProofSpiritLedger, Lot, TaxClass
from cellar.services import fortification as fort
from cellar.services import lotmeta


def _d(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def fort_ctx(lot, error=None, ok=None):
    events = list(lot.fortifications.filter(voided_at__isnull=True).order_by("booked_at", "id"))
    has_initial = any(e.kind == e.Kind.INITIAL for e in events)
    return {
        "lot": lot,
        "is_port": lotmeta.is_port(lot),
        "events": events,
        "has_initial": has_initial,
        "can_fortify_initial": not has_initial,
        "can_adjust": has_initial,
        "hpgs_on_hand_wg": HighProofSpiritLedger.on_hand_wg(),
        "hpgs_on_hand_pg": HighProofSpiritLedger.on_hand_pg(),
        "hpgs_blended_proof": HighProofSpiritLedger.current_blended_proof(),
        "tax_classes": TaxClass.choices,
        "today": timezone.localdate().isoformat(),
        "error": error,
        "ok": ok,
    }


@login_required
def lot_fortification(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_fortification.html", fort_ctx(lot))


@login_required
@require_http_methods(["POST"])
def fortification_preview(request, pk):
    """HTMX fragment: live Pearson-square preview as the target ABV / volume change."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        result = fort.pg_required(
            volume_gal=request.POST.get("volume_gal"),
            current_abv=request.POST.get("current_abv"),
            target_abv=request.POST.get("target_abv"),
            spirit_proof=request.POST.get("spirit_proof") or None,
        )
        error = None
    except Exception as e:  # noqa: BLE001
        result, error = None, str(e)
    return render(request, "web/_fortification_preview.html",
                  {"lot": lot, "result": result, "error": error})


@login_required
@require_http_methods(["POST"])
def lot_fortify_initial(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        fort.fortify(
            lot,
            fortified_on_skins_date=parse_date(request.POST.get("fortified_on_skins_date") or "")
                or timezone.localdate(),
            booked_at=parse_date(request.POST.get("booked_at") or "") or timezone.localdate(),
            proof_gallons_drawn=request.POST.get("proof_gallons_drawn"),
            finished_wg=request.POST.get("finished_wg") or None,
            target_abv=request.POST.get("target_abv") or None,
            expected_tax_class=request.POST.get("expected_tax_class") or None,
            spirit_proof=request.POST.get("spirit_proof") or None,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_fortification.html", fort_ctx(lot, error=str(e)))
    resp = render(request, "web/_lot_fortification.html",
                  fort_ctx(lot, ok=f"{lot.code} fortified on skins."))
    resp["HX-Refresh"] = "true"   # status/tab bar may change (booked to bond)
    return resp


@login_required
@require_http_methods(["POST"])
def lot_fortify_adjust(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        fort.adjust_alcohol(
            lot,
            adjusted_at=parse_date(request.POST.get("adjusted_at") or "") or timezone.localdate(),
            proof_gallons_drawn=request.POST.get("proof_gallons_drawn"),
            base_wg=request.POST.get("base_wg"),
            finished_wg=request.POST.get("finished_wg"),
            base_tax_class=request.POST.get("base_tax_class") or None,
            expected_tax_class=request.POST.get("expected_tax_class") or None,
            spirit_proof=request.POST.get("spirit_proof") or None,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_fortification.html", fort_ctx(lot, error=str(e)))
    return render(request, "web/_lot_fortification.html",
                  fort_ctx(lot, ok=f"{lot.code} alcohol adjustment recorded."))
