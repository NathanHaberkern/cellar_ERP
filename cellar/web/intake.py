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
    Variety, Block, Vessel, Additive, WeighTag, WeighTagBin, HarvestEvent, Lot,
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
        "additives": Additive.objects.exclude(dose_mode=Additive.DoseMode.BENCH)
                          .order_by("category", "name"),
        "severities": WeighTag._meta.get_field("mog_severity").choices,
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


# --------------------------------------------------- weigh-tag bins --
@login_required
def intake_tag_bins(request):
    """HTMX: the selected weigh tag's still-unassigned bin lines, as checkboxes to
    assign to this lot. Bins already crushed into another lot are shown disabled."""
    wt_id = (request.GET.get("weigh_tag") or "").strip()
    if not wt_id:
        return render(request, "web/_intake_tag_bins.html", {"bins": None})
    wt = get_object_or_404(WeighTag, pk=wt_id)
    return render(request, "web/_intake_tag_bins.html",
                  {"wt": wt, "bins": wt.bins.order_by("id")})


# -------------------------------------------------------------- destem --
@login_required
@require_http_methods(["POST"])
def intake_destem(request):
    P = request.POST
    try:
        variety = Variety.objects.filter(pk=(P.get("variety") or "").strip()).first()
        if not variety:
            raise ValueError("Choose a variety.")
        program = P.get("program") or Program.TABLE
        path = P.get("path")
        if not path:
            raise ValueError("Choose a processing path.")
        vintage = int(P.get("vintage") or (timezone.now().year % 100))
        raw_dt = P.get("destem_at")
        destem_at = (timezone.make_aware(datetime.fromisoformat(raw_dt))
                     if raw_dt else timezone.now())
        block = Block.objects.filter(pk=P.get("block")).first() if P.get("block") else None

        # ---- weigh tag + fruit source ----------------------------------------
        # Estate: assign specific bin lines (checkboxes). Purchased: net-only pounds.
        # Bins may span lots and a lot may span tags — both handled in the service.
        bin_ids = [b for b in P.getlist("bin_ids") if (b or "").strip().isdigit()]
        allocations = []
        net_lbs = None

        if P.get("weigh_tag"):
            wt = get_object_or_404(WeighTag, pk=P.get("weigh_tag"))
            if not bin_ids:                       # net-only draw from an existing tag
                net_lbs = _dec(P.get("net_lbs"))
                if not net_lbs:
                    raise ValueError("Enter net pounds, or check the bins to assign.")
                allocations = [(wt, net_lbs)]
        else:
            if block is None:
                raise ValueError("A block is required to create a new weigh tag.")
            he = HarvestEvent.objects.create(block=block, harvest_date=destem_at.date())
            number = ((P.get("new_tag_number") or "").strip()
                      or ops.generate_weigh_tag_number(block, destem_at.date()))
            wt = WeighTag.objects.create(
                weigh_tag_number=number, harvest_event=he,
                source_type=block.vineyard.grower.source_type,
                disposition=WeighTag.Disposition.CRUSHED,
                mog_severity=P.get("mog_severity") or "none",
                rot_severity=P.get("rot_severity") or "none",
                rot_type=(P.get("rot_type") or "").strip(),
                notes=(P.get("tag_notes") or "").strip(),
            )
            # optional per-bin lines (bin_label[] + bin_gross[] [+ bin_ct[]])
            made, labels, grosses, counts = [], P.getlist("bin_label"), P.getlist("bin_gross"), P.getlist("bin_ct")
            for i, (lbl, gr) in enumerate(zip(labels, grosses)):
                lbl, grd = (lbl or "").strip(), _dec(gr)
                if not lbl or grd is None:
                    continue
                ct = int(counts[i]) if i < len(counts) and (counts[i] or "").strip().isdigit() else 1
                made.append(WeighTagBin.objects.create(
                    weigh_tag=wt, bin_label=lbl, bin_count=ct, gross_lbs=grd))
            if made:
                bin_ids = [str(b.pk) for b in made]      # assign the new bins to this lot
            else:                                        # net-only new tag
                net_lbs = _dec(P.get("net_lbs"))
                if not net_lbs:
                    raise ValueError("Add bin lines, or enter net pounds.")
                wt.net_weight_lbs = net_lbs
                wt.save(update_fields=["net_weight_lbs"])
                allocations = [(wt, net_lbs)]

        # ---- vessel: tank or freshly-created A/B/C bins ----------------------
        tank_code = bins = None
        if P.get("into") == "bins":
            count = int(P.get("bin_count") or 1)
            size = Decimal(P.get("bin_size") or "1")
            bins = [size] * count
        else:
            tank_code = P.get("tank_code") or None
            if not tank_code:
                raise ValueError("Choose a destination tank.")

        foot_tread_pct = _dec(P.get("foot_tread_pct"))

        # Crusher additions entered up front — recorded atomically with the lot.
        # Blank rows are skipped, so an untouched "— choose —" never reaches a lookup.
        additions = []
        for aid, amt in zip(P.getlist("additive"), P.getlist("amount")):
            aid = (aid or "").strip()
            if not aid:
                continue
            add = Additive.objects.filter(pk=aid).first()
            if add:
                additions.append({"additive": add, "amount": _dec(amt)})

        r = ops.receive_and_destem(
            vintage=vintage, variety=variety, program=program, path=path,
            destem_at=destem_at,
            allocations=allocations or None,
            bin_ids=[int(x) for x in bin_ids] or None,
            block=block, tank_code=tank_code, bins=bins,
            crusher_enabled=(P.get("crusher_enabled") == "on"),
            foot_tread=(P.get("foot_tread") == "on"),
            foot_tread_pct=foot_tread_pct,
            initial_temp_f=_dec(P.get("initial_temp_f")),
            additions=additions,
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
    # Guard the empty select FIRST — an empty pk sent to a lookup is what threw
    # "Field 'id' expected a number but got ''" when the additive dropdown was blank.
    add_id = (request.GET.get("additive") or "").strip()
    if not add_id:
        return render(request, "web/_dose_preview.html", {})
    additive = get_object_or_404(Additive, pk=add_id)

    d = err = None
    try:
        override = _dec(request.GET.get("amount"))
        lot_id = (request.GET.get("lot") or "").strip()
        if lot_id:
            lot = get_object_or_404(Lot, pk=lot_id)
            d = ops.preview_addition(lot, additive, **_addition_kwargs(additive, override))
        else:
            # Pre-lot preview: dose off the live intake estimate (path + net lbs),
            # so the number shown up front equals what the atomic create will record.
            path = request.GET.get("path") or ""
            lbs = _dec(request.GET.get("net_lbs"))
            if not (path and lbs):
                raise ValueError("Pick a path and enter net pounds to preview a dose.")
            vol = ops.intake_volume_estimate(lbs, path)
            tons = ops.tons_from_lbs(lbs)
            d = ops.preview_dose(additive, volume_gal=vol, tons=tons,
                                 **_addition_kwargs(additive, override))
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
        aid = (request.POST.get("additive") or "").strip()
        if not aid:
            raise ValueError("Choose an additive.")
        additive = get_object_or_404(Additive, pk=aid)
        override = _dec(request.POST.get("amount"))
        a = ops.record_addition(lot, additive, added_at=timezone.now(),
                                **_addition_kwargs(additive, override))
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_intake_addition_row.html", {"error": str(e)})
    return render(request, "web/_intake_addition_row.html", {"a": a})
