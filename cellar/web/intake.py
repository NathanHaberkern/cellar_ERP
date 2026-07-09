"""
Guided receiving-fruit intake — HTMX views.

Renders the step-by-step destem flow and calls cellar/services/operations
directly (same pattern as the rest of web/: services, not the JSON API).

Flow:
  intake_index   GET   the receiving form (full page)
  intake_estimate GET  HTMX — live volume estimate as path + lbs change
  intake_destem  POST  HTMX — receive_and_destem(); swaps in the result panel
                       (lot summary + the additions sub-form bound to the lot)
  dose_preview   GET   HTMX — compute a dose WITHOUT writing, for live preview
  intake_addition POST HTMX — record_addition(); appends a ledger row
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cellar.models import (
    Variety, Block, Vessel, Additive, WeighTag, HarvestEvent, Lot,
    Program, DestemmingEvent,
)
from cellar.services import operations as ops


def _dec(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"'{raw}' is not a number.")


def _addition_kwargs(additive, override):
    if additive.dose_mode == Additive.DoseMode.PPM_TARGET:
        return {"target_ppm": override}
    if additive.dose_mode == Additive.DoseMode.BENCH:
        return {"explicit_quantity": override}
    return {"rate_override": override}


# ---------------------------------------------------------------- page --
@login_required
def intake_index(request):
    tanks = Vessel.objects.filter(type=Vessel.Type.TANK).order_by("code")
    open_tags = [wt for wt in WeighTag.objects.order_by("-created_at")[:80]
                 if wt.remaining_lbs and wt.remaining_lbs > 0]
    ctx = {
        "nav": "intake",
        "varieties": Variety.objects.order_by("name"),
        "programs": Program.choices,
        "paths": DestemmingEvent.Path.choices,
        "blocks": Block.objects.select_related("vineyard", "variety")
                       .order_by("vineyard__name", "name"),
        "tanks": tanks,
        "weigh_tags": open_tags,
        "default_vintage": timezone.now().year % 100,
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
    }
    return render(request, "web/intake.html", ctx)


# ------------------------------------------------------------ estimate --
@login_required
def intake_estimate(request):
    path = request.GET.get("path") or ""
    try:
        lbs = _dec(request.GET.get("net_lbs"))
    except ValueError:
        lbs = None
    est = tons = None
    if lbs and path:
        tons = ops.tons_from_lbs(lbs)
        est = ops.intake_volume_estimate(lbs, path)
    return render(request, "web/_intake_estimate.html",
                  {"est": est, "tons": tons, "path": path})


# -------------------------------------------------------------- destem --
@login_required
@require_http_methods(["POST"])
def intake_destem(request):
    P = request.POST
    try:
        variety = get_object_or_404(Variety, pk=P.get("variety"))
        program = P.get("program") or Program.TABLE
        path = P.get("path")
        if not path:
            raise ValueError("Choose a processing path.")
        vintage = int(P.get("vintage") or (timezone.now().year % 100))
        raw_dt = P.get("destem_at")
        destem_at = (timezone.make_aware(datetime.fromisoformat(raw_dt))
                     if raw_dt else timezone.now())
        block = Block.objects.filter(pk=P.get("block")).first() if P.get("block") else None

        net_lbs = _dec(P.get("net_lbs"))
        if not net_lbs:
            raise ValueError("Net pounds are required.")

        # weigh tag: use the selected one, or quick-create from a new number
        if P.get("weigh_tag"):
            wt = get_object_or_404(WeighTag, pk=P.get("weigh_tag"))
        else:
            num = (P.get("new_tag_number") or "").strip()
            if not num:
                raise ValueError("Select a weigh tag or enter a new tag number.")
            if block is None:
                raise ValueError("A block is required to create a new weigh tag.")
            he = HarvestEvent.objects.create(block=block, harvest_date=destem_at.date())
            wt = WeighTag.objects.create(
                weigh_tag_number=num, harvest_event=he,
                source_type=block.vineyard.grower.source_type,
                disposition=WeighTag.Disposition.CRUSHED, net_weight_lbs=net_lbs)

        # vessel: tank or freshly-created A/B/C bins
        tank_code = bins = None
        if P.get("into") == "bins":
            count = int(P.get("bin_count") or 1)
            size = Decimal(P.get("bin_size") or "1")
            bins = [size] * count
        else:
            tank_code = P.get("tank_code") or None
            if not tank_code:
                raise ValueError("Choose a destination tank.")

        r = ops.receive_and_destem(
            vintage=vintage, variety=variety, program=program, path=path,
            destem_at=destem_at, allocations=[(wt, net_lbs)], block=block,
            tank_code=tank_code, bins=bins,
            crusher_enabled=(P.get("crusher_enabled") == "on"),
            foot_tread=(P.get("foot_tread") == "on"),
            initial_temp_f=_dec(P.get("initial_temp_f")),
        )
    except Exception as e:  # noqa: BLE001 — surface at the UI seam
        return render(request, "web/_intake_result.html", {"error": str(e)})

    lot = r["lot"]
    return render(request, "web/_intake_result.html", {
        "r": r, "lot": lot,
        "additives": Additive.objects.exclude(dose_mode=Additive.DoseMode.BENCH)
                          .order_by("category", "name"),
        "additions": lot.additions.filter(voided_at__isnull=True).order_by("id"),
    })


# ---------------------------------------------------------- dose preview --
@login_required
def dose_preview(request):
    add_id = request.GET.get("additive")
    lot = get_object_or_404(Lot, pk=request.GET.get("lot"))
    if not add_id:
        return render(request, "web/_dose_preview.html", {})
    additive = get_object_or_404(Additive, pk=add_id)
    d = err = None
    try:
        override = _dec(request.GET.get("amount"))
        d = ops.preview_addition(lot, additive, **_addition_kwargs(additive, override))
    except Exception as e:  # noqa: BLE001
        err = str(e)
    default_amt = (additive.default_target_ppm
                   if additive.dose_mode == Additive.DoseMode.PPM_TARGET
                   else additive.default_rate)
    hint = ("ppm" if additive.dose_mode == Additive.DoseMode.PPM_TARGET
            else (additive.rate_unit or additive.unit))
    return render(request, "web/_dose_preview.html",
                  {"additive": additive, "d": d, "err": err,
                   "default_amt": default_amt, "unit_hint": hint})


# ------------------------------------------------------------ addition --
@login_required
@require_http_methods(["POST"])
def intake_addition(request, lot_pk):
    lot = get_object_or_404(Lot, pk=lot_pk)
    try:
        additive = get_object_or_404(Additive, pk=request.POST.get("additive"))
        override = _dec(request.POST.get("amount"))
        a = ops.record_addition(lot, additive, added_at=timezone.now(),
                                **_addition_kwargs(additive, override))
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_intake_addition_row.html", {"error": str(e)})
    return render(request, "web/_intake_addition_row.html", {"a": a})
