"""
Back-sweetening — front end (HTMX), lives on the lot page as its own tab.

`SweeteningEvent` and `services/sweetening.py` both already existed and carry the
full 5120.17 shape (wine used on line 18, sweetened wine produced on line 3, plus
the Part IV concentrate material use). There was simply no way to reach either
from the app, so ".25% RS Vino Blanc" had nowhere to be entered.

The form takes a target RS % (how the cellar actually thinks) OR explicit gallons
of concentrate, and previews the dose live before anything is written.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cellar.models import Lot
from cellar.services import sweetening as sw
from cellar.services import volumes as vol_svc
from . import lotpages


def _dec(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"Couldn't read the number '{raw}'.")


def _parse_dt(raw):
    from django.utils.dateparse import parse_datetime
    raw = (raw or "").strip()
    if not raw:
        return timezone.now()
    dt = parse_datetime(raw)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt or timezone.now()


def sweeten_ctx(lot, *, error=None, ok=None):
    concentrate_missing = None
    try:
        sw.vino_blanc_material()
    except sw.ConcentrateNotFound as e:
        concentrate_missing = str(e)

    return {
        "lot": lot,
        "section": "sweetening",
        "note": lotpages.section_note(lot, "sweetening"),
        "events": sw.sweetenings_of(lot),
        "current_balance": vol_svc.working_volume(lot),
        "default_brix": sw.DEFAULT_CONCENTRATE_BRIX,
        "concentrate_missing": concentrate_missing,
        "error": error,
        "ok": ok,
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
    }


@login_required
def lot_sweeten(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_sweeten.html", sweeten_ctx(lot))


@login_required
def sweeten_preview(request, pk):
    """Live preview: how many gallons of concentrate a target RS % works out to,
    and what the tank reads afterwards."""
    lot = get_object_or_404(Lot, pk=pk)
    ctx = {"lot": lot}
    try:
        wine = vol_svc.working_volume(lot)
        if wine is None:
            raise ValueError(f"{lot.code} has no recorded volume to sweeten.")
        gal = _dec(request.GET.get("concentrate_gal"))
        if gal is None:
            target = _dec(request.GET.get("target_rs_pct"))
            if target is None:
                return render(request, "web/_sweeten_preview.html", {})
            brix = _dec(request.GET.get("concentrate_brix")) or sw.DEFAULT_CONCENTRATE_BRIX
            gal = sw.concentrate_gallons_for_rs(wine, target, brix)
            ctx["derived"] = True
        ctx.update({
            "wine_gal": wine,
            "conc_gal": gal,
            "finished_gal": (wine + gal).quantize(Decimal("0.1")),
        })
    except Exception as e:  # noqa: BLE001
        ctx["err"] = str(e)
    return render(request, "web/_sweeten_preview.html", ctx)


@login_required
@require_http_methods(["POST"])
def lot_sweeten_create(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        ev = sw.sweeten(
            lot,
            sweetened_at=_parse_dt(request.POST.get("sweetened_at")),
            concentrate_gal=_dec(request.POST.get("concentrate_gal")),
            target_rs_pct=_dec(request.POST.get("target_rs_pct")),
            concentrate_brix=(_dec(request.POST.get("concentrate_brix"))
                              or sw.DEFAULT_CONCENTRATE_BRIX),
            brix_before=_dec(request.POST.get("brix_before")),
            brix_after=_dec(request.POST.get("brix_after")),
            actor=request.user)
        lot.refresh_from_db()
        ok = (f"Sweetened {ev.volume_used} gal with {ev.concentrate_gallons} gal "
              f"{ev.concentrate.name} — {lot.code} now reads {ev.volume_produced} gal.")
        return render(request, "web/_lot_sweeten.html", sweeten_ctx(lot, ok=ok))
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_lot_sweeten.html", sweeten_ctx(lot, error=str(e)))
