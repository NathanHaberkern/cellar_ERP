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


def _effective_net_lbs(request):
    """Net-pounds basis for the pre-lot dose preview, from whichever fruit source the
    form has so far: checked existing-tag bins, unsaved new-tag bin lines, or the
    selected tag's remaining pounds. Returns a Decimal or None."""
    G = request.GET
    ids = [b for b in G.getlist("bin_ids") if (b or "").strip().isdigit()]
    if ids:
        total = sum((b.net_lbs or 0) for b in WeighTagBin.objects.filter(pk__in=ids))
        if total:
            return Decimal(total)
    # New-tag bin lines. The form emits these PER ROW as bin_gross_<rid> /
    # bin_ct_<rid> (the commit path in intake_destem reads exactly those names), so
    # reading a bare "bin_gross" list finds nothing and the preview reports "enter
    # net pounds" even though the same lines book fine on submit. Pair by row id.
    total = Decimal("0")
    for key, raw in G.items():
        if not key.startswith("bin_gross_"):
            continue
        g = _dec(raw)
        if g is None:
            continue
        rid = key[len("bin_gross_"):]
        ct_raw = (G.get(f"bin_ct_{rid}") or "").strip()
        ct = int(ct_raw) if ct_raw.isdigit() else 1
        total += g - ct * WeighTagBin.TARE_PER_BIN
    if total > 0:
        return total
    wt_id = (G.get("weigh_tag") or "").strip()
    if wt_id:
        wt = WeighTag.objects.filter(pk=wt_id).first()
        if wt:
            return (wt.remaining_lbs or wt.net_total) or None
    return None


# ---------------------------------------------------------------- page --
@login_required
def intake_index(request):
    from .tankmap import _open_assignments
    occupied = set(_open_assignments().keys())
    tanks = [t for t in Vessel.objects.filter(type=Vessel.Type.TANK).order_by("code")
             if t.id not in occupied]
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
            # per-bin lines (indexed by a client-side manifest so each line's
            # include-checkbox aligns with its own inputs). Checked → assign to
            # this lot; unchecked → created but held as unassigned fruit.
            made_assign = []
            for rid in [r for r in (P.get("bin_rows") or "").split(",") if r.strip()]:
                lbl = (P.get(f"bin_label_{rid}") or "").strip()
                grd = _dec(P.get(f"bin_gross_{rid}"))
                if not lbl or grd is None:
                    continue
                ct = int(P.get(f"bin_ct_{rid}")) if (P.get(f"bin_ct_{rid}") or "").isdigit() else 1
                b = WeighTagBin.objects.create(weigh_tag=wt, bin_label=lbl, bin_count=ct, gross_lbs=grd)
                if P.get(f"bin_incl_{rid}"):        # checkbox present ⇒ assign
                    made_assign.append(b)
            if made_assign:
                bin_ids = [str(b.pk) for b in made_assign]
            else:                                   # no assigned lines ⇒ net-only
                net_lbs = _dec(P.get("net_lbs"))
                if not net_lbs:
                    raise ValueError("Check at least one bin line to assign, or enter net pounds.")
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
    resp = render(request, "web/_intake_result.html", {
        "r": r, "lot": lot,
        "additives": Additive.objects.exclude(dose_mode=Additive.DoseMode.BENCH)
                          .order_by("category", "name"),
        "additions": lot.additions.filter(voided_at__isnull=True).order_by("id"),
    })
    # Swap the whole form out for the summary so the same entry can't be submitted
    # twice (append-only ⇒ a resubmit would create a duplicate lot).
    resp["HX-Retarget"] = "#intake-form"
    resp["HX-Reswap"] = "outerHTML"
    return resp


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
            # Pre-lot preview: derive the volume basis from whatever fruit source the
            # form has so far — typed net pounds, checked bins, new-tag bin lines, or
            # the selected tag's remaining pounds.
            path = request.GET.get("path") or ""
            lbs = _dec(request.GET.get("net_lbs")) or _effective_net_lbs(request)
            if not path:
                raise ValueError("Pick a processing path to preview a dose.")
            if not lbs:
                raise ValueError("Enter net pounds, check bins, or add bin lines to preview.")
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
