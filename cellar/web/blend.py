"""
Blending — front end (HTMX). Lives on the Movement tab, next to the plain tank
transfer form, since a blend both moves wine and writes the compliance edge
that transfer_lot(allow_blend=True) alone never did — see services/blending.py.

The form takes ONE destination lot and one or more source rows (lot + kind +
gallons for a partial). Cross-tax-class blends are allowed — that's exactly
the change-of-tax-class scenario partx.py already narrates — but the preview
step flags the mismatch so it's a deliberate choice, not an accident.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Lot, LotLineage, Vessel
from cellar.services import blending as bl


def _d(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def blend_source_lots(exclude_lot):
    from .topping import topping_source_lots
    return topping_source_lots(exclude_lot=exclude_lot)


def blend_ctx(lot, error=None, ok=None, preview=None):
    from .vessels import vessel_options
    return {
        "lot": lot,
        "sources": blend_source_lots(lot),
        "vessel_options": vessel_options(exclude_lot=lot),
        "today": timezone.localdate().isoformat(),
        "error": error, "ok": ok, "preview": preview,
    }


@login_required
def lot_blend_card(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_blend.html", blend_ctx(lot))


@login_required
@require_http_methods(["POST"])
def blend_preview(request, pk):
    """HTMX fragment: balances + cross-class warning for one source row before commit."""
    lot = get_object_or_404(Lot, pk=pk)
    source_pk = request.POST.get("source_lot")
    kind = request.POST.get("kind") or LotLineage.Relationship.WHOLE_BLEND
    volume_gal = request.POST.get("volume_gal")
    result, error = None, None
    try:
        source = Lot.objects.get(pk=source_pk)
        result = bl.preview(source, lot, kind=kind, volume_gal=volume_gal)
        result["source_code"] = source.code
    except Exception as e:  # noqa: BLE001
        error = str(e)
    return render(request, "web/_blend_preview.html", {"lot": lot, "result": result, "error": error})


@login_required
@require_http_methods(["POST"])
def lot_blend_commit(request, pk):
    """Commit one or more source rows into this lot in a single blend session.

    Rows arrive as parallel POST lists: source_lot[], kind[], volume_gal[].
    A blank volume_gal is fine for WHOLE_BLEND (ignored) but required for
    PARTIAL_BLEND — blend_many() surfaces that as a per-row ValueError.
    """
    lot = get_object_or_404(Lot, pk=pk)
    error = None
    try:
        source_pks = request.POST.getlist("source_lot")
        kinds = request.POST.getlist("kind")
        vols = request.POST.getlist("volume_gal")
        if not source_pks:
            raise ValueError("Add at least one source lot to blend.")

        to_vessel_pk = request.POST.get("to_vessel") or None
        to_vessel = Vessel.objects.get(pk=to_vessel_pk) if to_vessel_pk else None
        blended_at = parse_date(request.POST.get("blended_at") or "") or timezone.localdate()

        sources = []
        for i, spk in enumerate(source_pks):
            if not spk:
                continue
            source = Lot.objects.get(pk=spk)
            kind = kinds[i] if i < len(kinds) else LotLineage.Relationship.WHOLE_BLEND
            vol = vols[i] if i < len(vols) else None
            sources.append((source, kind, vol))

        bl.blend_many(sources, lot, blended_at=blended_at, to_vessel=to_vessel, actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        error = str(e)

    from . import views
    return views.lot_movement_with_error(request, pk, error) if error else views.lot_movement(request, pk)
