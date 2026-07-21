"""
Server-rendered HTMX front end for the Cellar ERP.

Pattern: these views render HTML (full pages, or fragments for HTMX swaps) and
call cellar/services/ DIRECTLY -- they do NOT go over the DRF JSON API. The JSON
API (cellar/api/) is for the future iOS client; the browser talks to these views
using the same session auth already configured. One services layer, two consumers.

HTMX usage here:
  - live lot search        -> GET fragment swapped into the results tbody
  - lot detail sub-panels  -> composition / oak / cost lazy-loaded as fragments
  - report rendering       -> POST period, swap the rendered report in place
  - additive CRUD          -> inline add / edit / void without full page reloads

Service calls are bound to the real cellar/services signatures and wrapped so a
data gap shows an inline message rather than 500-ing the page.
"""

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cellar.models.spine import Lot, LotSectionNote
from cellar.models.reference import Additive, Vessel, LabAnalyte
from cellar.models.fermentation import LabResult, LabResultValue

from cellar.services import aging as aging_svc
from cellar.services import costing as costing_svc
from cellar.services import reporting as reporting_svc
from cellar.services import excise as excise_svc
from cellar.services import crush_report as crush_svc
from cellar.services import operations as ops
from cellar.services import labpanels
from cellar.services import labimport
from .tankmap import build_tank_map
from . import lotpages


def _htmx(request):
    return request.headers.get("HX-Request") == "true"


def _safe(fn, *args, **kwargs):
    """Run a service call; return (result, error_message). Keeps a signature
    mismatch or data gap from 500-ing the page -- the template shows the message."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - deliberately broad at the UI seam
        return None, f"{fn.__module__}.{fn.__name__}: {e}"


# ---------------------------------------------------------------- dashboard --
@login_required
def dashboard(request):
    from cellar.services import dashboard_mode, cellar_board
    active_lots = Lot.objects.count()
    mode = dashboard_mode.detect_mode()
    ctx = {
        "nav": "dashboard",
        "active_lots": active_lots,
        "today": timezone.localdate(),
        "mode": mode,
    }
    if mode == "crush":
        tank_map, tank_map_error = _safe(build_tank_map)
        ctx.update({"tank_map": tank_map or [], "tank_map_error": tank_map_error})
    else:
        board, board_error = _safe(cellar_board.board_context)
        ctx.update({"board": board or {}, "board_error": board_error})
    from . import tasks as tasks_web
    ctx.update(tasks_web.dash_tasks_ctx(request))
    return render(request, "web/dashboard.html", ctx)


# --------------------------------------------------------------------- lots --
@login_required
def lots_list(request):
    ctx = {"nav": "lots", "lots": _lot_queryset(request)}
    ctx.update(_lot_filter_ctx(request))
    return render(request, "web/lots_list.html", ctx)


@login_required
def lots_search(request):
    """HTMX fragment: just the table body, swapped as the user types."""
    return render(request, "web/_lots_rows.html", {"lots": _lot_queryset(request)})


def _lot_queryset(request):
    """Text search + status / disposition / vessel filters.

    Lot.code and disposition are both derived (code from current_designation,
    disposition from the BookToBond / FortificationEvent ledger), so those two
    filter in Python. Status and vessel are real columns and filter in SQL. Fine at
    this scale — a vintage is a few hundred lots.
    """
    from cellar.services import bonding as bond
    from cellar.models import TankAssignment

    qs = Lot.objects.select_related("current_designation").order_by("-pk")

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    vessel = (request.GET.get("vessel") or "").strip()
    if vessel:
        lot_ids = (TankAssignment.objects
                   .filter(voided_at__isnull=True, emptied_at__isnull=True,
                           vessel__code=vessel)
                   .values_list("lot_id", flat=True))
        qs = qs.filter(pk__in=list(lot_ids))

    lots = list(qs[:400])

    q = (request.GET.get("q") or "").strip().lower()
    if q:
        lots = [lot for lot in lots if q in (lot.code or "").lower()]

    disp = (request.GET.get("disposition") or "").strip()
    if disp == "in_bond":
        lots = [lot for lot in lots if bond.is_in_bond(lot)]
    elif disp == "not_booked":
        lots = [lot for lot in lots
                if not bond.is_in_bond(lot)
                and lot.status in (Lot.Status.PRESSED, Lot.Status.SETTLING)]
    elif disp == "in_fermenter":
        lots = [lot for lot in lots
                if not bond.is_in_bond(lot)
                and lot.status not in (Lot.Status.PRESSED, Lot.Status.SETTLING)]

    # decorate for the row template (both are derived, so compute once here)
    for lot in lots:
        lot.disposition = lotpages.disposition(lot)
        lot.location = lotpages.current_location(lot)
    return lots[:200]


def _lot_filter_ctx(request):
    from cellar.models import Vessel
    return {
        "q": request.GET.get("q", ""),
        "f_status": request.GET.get("status", ""),
        "f_disposition": request.GET.get("disposition", ""),
        "f_vessel": request.GET.get("vessel", ""),
        "statuses": Lot.Status.choices,
        "vessels": Vessel.objects.order_by("code").values_list("code", flat=True),
    }


@login_required
@require_http_methods(["POST"])
def lot_skin_contact_override_save(request, pk):
    """Save (or clear, on blank) the per-lot minimum skin-contact override.
    Always editable from the lot page — see LotFermentationOverride's
    docstring for why this is separate from the append-only destemming record.
    Plain form POST that redirects back to the lot (now the v2 Fermentation
    tile), rather than re-rendering a fragment directly.
    """
    from django.shortcuts import redirect
    from cellar.models import LotFermentationOverride
    lot = get_object_or_404(Lot, pk=pk)
    raw = (request.POST.get("min_skin_contact_days") or "").strip()
    if raw == "":
        LotFermentationOverride.objects.filter(lot=lot).delete()
    else:
        try:
            days = int(raw)
            if days < 0:
                raise ValueError
        except ValueError:
            return redirect("lot-detail", pk=pk)
        LotFermentationOverride.objects.update_or_create(
            lot=lot, defaults={"min_skin_contact_days": days, "updated_by": request.user})
    return redirect("lot-detail", pk=pk)


# -- sub-panels (HTMX fragments swapped into #lot-panel) ---------------------
def _panel(request, pk, section, template, extra):
    """Render one sub-panel with its section scratchpad note attached."""
    lot = get_object_or_404(Lot, pk=pk)
    ctx = {"lot": lot, "section": section,
           "note": lotpages.section_note(lot, section)}
    ctx.update(extra(lot))
    return render(request, template, ctx)


@login_required
def lot_additions(request, pk):
    def extra(lot):
        rows, err = _safe(lotpages.additions, lot)
        return {"rows": rows or [], "error": err,
                "additives": Additive.objects.exclude(dose_mode=Additive.DoseMode.BENCH)
                                     .order_by("category", "name"),
                "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    return _panel(request, pk, "additions", "web/_lot_additions.html", extra)


@login_required
def lot_labs(request, pk):
    def extra(lot):
        groups, err = _safe(lotpages.labs, lot)
        return {"groups": groups or [], "error": err,
                "sources": LabResult.Source.choices,
                "analytes": LabAnalyte.objects.order_by("name"),
                "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    return _panel(request, pk, "labs", "web/_lot_labs.html", extra)


@login_required
def lot_movement(request, pk):
    def extra(lot):
        rows, err = _safe(lotpages.movements, lot)
        from .vessels import vessel_options
        from .blend import blend_source_lots
        from cellar.models import ExternalDestination
        from cellar.services import volumes as vol_svc
        # Every tank/tote is offered; occupied ones are shown with their current lot
        # and unlocked only by the co-occupancy checkbox. Filtering them out here is
        # what made that checkbox dead.
        return {"rows": rows or [], "error": err,
                "vessel_options": vessel_options(exclude_lot=lot),
                "blend_sources": blend_source_lots(exclude_lot=lot),
                "external_destinations": ExternalDestination.objects.order_by("name"),
                "current_balance": vol_svc.lot_balance(lot),
                "today": timezone.localdate().isoformat(),
                "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    return _panel(request, pk, "movement", "web/_lot_movement.html", extra)


@login_required
def lot_composition(request, pk):
    def extra(lot):
        data, err = _safe(lotpages.composition, lot)
        override = getattr(lot, "composition_override", None)
        return {"composition": data or {}, "error": err, "override": override}
    return _panel(request, pk, "composition", "web/_lot_composition.html", extra)


@login_required
@require_http_methods(["POST"])
def lot_composition_override_save(request, pk):
    """Save (or clear) the manual label/marketing composition. Entirely separate
    from the computed genealogy above it — never feeds reporting."""
    from cellar.models import LotCompositionOverride
    lot = get_object_or_404(Lot, pk=pk)
    labels = request.POST.getlist("label")
    pcts = request.POST.getlist("pct")
    components = []
    for label, pct in zip(labels, pcts):
        label = (label or "").strip()
        pct = (pct or "").strip()
        if label and pct:
            try:
                components.append({"label": label, "pct": float(pct)})
            except ValueError:
                continue
    notes = (request.POST.get("notes") or "").strip()

    if not components:
        LotCompositionOverride.objects.filter(lot=lot).delete()
    else:
        LotCompositionOverride.objects.update_or_create(
            lot=lot, defaults={"components": components, "notes": notes,
                               "updated_by": request.user})
    return lot_composition(request, pk)


def render_oak_panel(request, lot, error=None):
    """The Oak tab, rendered from a lot we already have. Shared by the tab itself
    and by the barrel-down/topping/rack-out POSTs, which swap this panel back in."""
    from cellar.services import barreling as bar
    from cellar.services import bonding as bond
    from cellar.models import Container, TankAssignment
    from . import topping as top_web
    from . import vessels as vessels_web

    data, err = _safe(lotpages.oak, lot)
    in_tank = TankAssignment.objects.filter(
        lot=lot, voided_at__isnull=True, emptied_at__isnull=True).exists()
    return render(request, "web/_lot_oak.html", {
        "lot": lot, "section": "oak",
        "note": lotpages.section_note(lot, "oak"),
        "oak": data or {}, "error": error or err,
        # barrel-down form: the picker searches/scans (see oak_barrel_search) rather
        # than rendering every empty barrel — at 1000+ barrels that list is unusable.
        # These are just the filter dropdown options, which are small and cheap.
        "barrel_types": [(Container.Type.BARREL, "Barrel"), (Container.Type.FOUDRE, "Foudre")],
        "barrel_formats": bar.empty_oak_formats(),
        "can_rack": lot.status not in (Lot.Status.PLANNED, Lot.Status.BOTTLED),
        "still_in_tank": in_tank,
        "barrel_total": bond.barrel_fill_total(lot),
        "in_bond": bond.is_in_bond(lot),
        "today": timezone.localdate().isoformat(),
        # topping / rack-out forms — operate on THIS lot's own filled barrels
        "topping_sources": top_web.topping_source_lots(exclude_lot=None),
        "vessel_options": vessels_web.vessel_options(exclude_lot=lot),
    })


@login_required
def lot_oak(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render_oak_panel(request, lot)


@login_required
def lot_cost(request, pk):
    def extra(lot):
        breakdown, err = _safe(lambda l: {
            "fruit": costing_svc.fruit_cost(l),
            "fruit_trueup": costing_svc.fruit_trueup_cost(l),
            "additions": costing_svc.addition_cost(l),
            "spirit": costing_svc.spirit_cost(l),
            "oak_depreciation": costing_svc.lot_oak_depreciation(l),
            "adjustments": costing_svc.adjustment_cost(l),
            "total": costing_svc.lot_cost(l),
            "per_gal": costing_svc.lot_cost_per_gal(l),
        }, lot)
        return {"cost": breakdown or {}, "error": err}
    return _panel(request, pk, "cost", "web/_lot_cost.html", extra)


@login_required
def lot_tasks(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    from . import tasks as tasks_web
    return render(request, "web/_lot_tasks.html", tasks_web.lot_tasks_ctx(lot))


# -- section note save (mutable scratchpad) ---------------------------------
@login_required
@require_http_methods(["POST"])
def lot_note_save(request, pk, section):
    lot = get_object_or_404(Lot, pk=pk)
    valid = {s for s, _ in LotSectionNote.Section.choices}
    if section not in valid:
        return render(request, "web/_lot_note.html",
                      {"lot": lot, "section": section, "note": "",
                       "note_error": "Unknown section."}, status=400)
    lotpages.save_section_note(lot, section, request.POST.get("body", ""), request.user)
    return render(request, "web/_lot_note.html",
                  {"lot": lot, "section": section,
                   "note": request.POST.get("body", ""), "saved": True})


# -- entry actions on the sub-pages -----------------------------------------
@login_required
@require_http_methods(["POST"])
def lot_addition_create(request, pk):
    from cellar.models.ledger import Addition
    lot = get_object_or_404(Lot, pk=pk)
    try:
        aid = (request.POST.get("additive") or "").strip()
        if not aid:
            raise ValueError("Choose an additive.")
        additive = get_object_or_404(Additive, pk=aid)
        added_at = _parse_dt(request.POST.get("added_at"))
        # One "amount" box, mapped to the right dose kwarg for this additive's mode
        # (ppm target / bench quantity / rate / percent) — the same helper the intake
        # form uses. The old two-box rate+target_ppm form couldn't express a percent
        # dose at all, and neither box drove a preview.
        raw = (request.POST.get("amount") or "").strip()
        override = Decimal(raw) if raw else None
        a = ops.record_addition(lot, additive, added_at=added_at,
                                **ops._addition_kwargs(additive, override))
        note = (request.POST.get("note") or "").strip()
        if note:
            # notes isn't editable through the append-only save guard; set it via
            # a direct update at creation, the same pattern scan.py uses to close rows.
            Addition.objects.filter(pk=a.pk).update(notes=note)
    except Exception as e:  # noqa: BLE001
        return lot_additions_with_error(request, pk, str(e))
    return lot_additions(request, pk)


@login_required
@require_http_methods(["POST"])
def lot_lab_create(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        source = request.POST.get("source") or LabResult.Source.ETS
        sample_id = (request.POST.get("sample_id") or "").strip()
        if source in (LabResult.Source.ETS, LabResult.Source.LODI) and not sample_id:
            raise ValueError("Sample ID is required for ETS and Lodi Wine Labs results.")
        if source == LabResult.Source.IN_HOUSE:
            sample_id = ""
        # analyte/value pairs arrive as parallel lists analyte[]/value[]
        analytes = request.POST.getlist("analyte")
        values = request.POST.getlist("value")
        pairs = [(int(aid), (val or "").strip())
                 for aid, val in zip(analytes, values) if aid and val not in (None, "")]
        if not pairs:
            raise ValueError("Enter at least one analyte value.")
        analyte_by_id = {a.pk: a for a in LabAnalyte.objects.filter(pk__in=[p[0] for p in pairs])}
        panel = labpanels.classify([analyte_by_id[aid].slug for aid, _ in pairs
                                    if aid in analyte_by_id])
        op = request.user if request.user.is_authenticated else None
        result = LabResult.objects.create(
            lot=lot, reported_at=_parse_dt(request.POST.get("reported_at")),
            source=source, panel=panel, sample_id=sample_id,
            notes=request.POST.get("note", ""), operator=op)
        for aid, raw in pairs:
            a = analyte_by_id.get(aid)
            if a is None:
                continue
            v, qual, flag, disp = labimport.parse_result(raw, a.slug)
            LabResultValue.objects.create(
                result=result, analyte=a, value=round(v, 3), qualifier=qual,
                flag=flag, display=disp, raw_result=raw, operator=op)
    except Exception as e:  # noqa: BLE001
        return lot_labs_with_error(request, pk, str(e))
    return lot_labs(request, pk)


@login_required
@require_http_methods(["POST"])
def lot_transfer_create(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        vessel = get_object_or_404(Vessel, pk=request.POST.get("to_vessel"))
        at = _parse_dt(request.POST.get("moved_at"))
        assignment = ops.transfer_lot(lot, vessel, at)
        if request.POST.get("note"):
            type(assignment).objects.filter(pk=assignment.pk).update(notes=request.POST["note"])
    except Exception as e:  # noqa: BLE001
        return lot_movement_with_error(request, pk, str(e))
    return lot_movement(request, pk)


@login_required
@require_http_methods(["POST"])
def lot_split_create(request, pk):
    """Partial transfer: move part of a lot into another vessel as a NEW lot.
    See services/splitting.py — this is how 25VERD → 25VERDPORT gets made."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        from cellar.services import splitting
        vessel = get_object_or_404(Vessel, pk=request.POST.get("to_vessel"))
        program = (request.POST.get("program") or "").strip() or None
        child = splitting.split_lot(
            lot, volume_gal=request.POST.get("volume_gal"), to_vessel=vessel,
            at=_parse_dt(request.POST.get("moved_at")), program=program,
            note=request.POST.get("note", ""), actor=request.user)
        lot.refresh_from_db()
        ok = (f"Split {request.POST.get('volume_gal')} gal off {lot.code} into "
              f"{child.code} in {vessel.code}. {lot.code} keeps the remainder.")
    except Exception as e:  # noqa: BLE001
        return lot_movement_with_error(request, pk, str(e))

    def extra(l):
        rows, err = _safe(lotpages.movements, l)
        from .vessels import vessel_options
        from .blend import blend_source_lots
        from cellar.models import ExternalDestination as ED
        from cellar.services import volumes as vol_svc
        return {"rows": rows or [], "error": err, "ok": ok,
                "vessel_options": vessel_options(exclude_lot=l),
                "blend_sources": blend_source_lots(exclude_lot=l),
                "external_destinations": ED.objects.order_by("name"),
                "current_balance": vol_svc.lot_balance(l),
                "today": timezone.localdate().isoformat(),
                "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    return _panel(request, pk, "movement", "web/_lot_movement.html", extra)


@login_required
@require_http_methods(["POST"])
def lot_external_transfer_create(request, pk):
    """Book a lot leaving the winery — a bulk taxpaid sale or an in-bond move
    to another bonded premises. See services/external_transfer.py: juice and
    grapes (never inoculated) skip the 5120.17 entry; anything that has
    started fermenting gets one written in the same action."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        from cellar.models import ExternalDestination
        from cellar.services import external_transfer as ext

        dest = get_object_or_404(ExternalDestination, pk=request.POST.get("destination"))
        kind = request.POST.get("kind") or "taxpaid"
        result = ext.book_external_sale(
            lot, destination=dest, gallons=request.POST.get("gallons"),
            at=_parse_dt(request.POST.get("transferred_at")), kind=kind,
            channel=request.POST.get("channel") or None,
            note=request.POST.get("note", ""), actor=request.user)
        extent = "The lot is now empty." if result["full_sale"] else \
                 "The remaining balance stays in its current vessel."
        if kind == ext.KIND_MUST_SALE:
            ok = (f"Recorded {result['gallons']} gal must/juice sale to {dest.name}. "
                  f"No 5120.17 entry needed — {lot.code} is still juice/must "
                  f"(never inoculated). {extent}")
        elif result["wine"]:
            kind_label = "in-bond transfer" if kind == ext.KIND_IN_BOND else "bulk taxpaid removal"
            ok = (f"Recorded {result['gallons']} gal to {dest.name} — the 5120.17 "
                  f"{kind_label} entry has been written. {extent}")
        else:
            ok = (f"Recorded {result['gallons']} gal to {dest.name}. No 5120.17 entry "
                  f"needed — {lot.code} is still juice/grapes (never inoculated). {extent}")
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return lot_movement_with_error(request, pk, str(e))

    def extra(l):
        rows, err = _safe(lotpages.movements, l)
        from .vessels import vessel_options
        from .blend import blend_source_lots
        from cellar.models import ExternalDestination as ED
        from cellar.services import volumes as vol_svc
        return {"rows": rows or [], "error": err, "ok": ok,
                "vessel_options": vessel_options(exclude_lot=l),
                "blend_sources": blend_source_lots(exclude_lot=l),
                "external_destinations": ED.objects.order_by("name"),
                "current_balance": vol_svc.lot_balance(l),
                "today": timezone.localdate().isoformat(),
                "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    return _panel(request, pk, "movement", "web/_lot_movement.html", extra)


def _parse_dt(raw):
    from datetime import datetime as _dt
    from django.utils.dateparse import parse_datetime, parse_date
    raw = (raw or "").strip()
    if not raw:
        return timezone.now()
    dt = parse_datetime(raw)
    if dt is None:
        d = parse_date(raw)
        if d is None:
            raise ValueError(f"Couldn't read the date/time '{raw}'.")
        dt = _dt(d.year, d.month, d.day)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


# error re-renders keep the panel visible with the message inline
def lot_additions_with_error(request, pk, msg):
    resp = lot_additions(request, pk)
    return _inject_error(request, pk, "web/_lot_additions.html", resp, msg, lotpages.additions, "rows",
                         {"additives": Additive.objects.exclude(dose_mode=Additive.DoseMode.BENCH)
                                               .order_by("category", "name"),
                          "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")})


def lot_labs_with_error(request, pk, msg):
    return _inject_error(request, pk, "web/_lot_labs.html", None, msg, lotpages.labs, "groups",
                         {"sources": LabResult.Source.choices,
                          "analytes": LabAnalyte.objects.order_by("name"),
                          "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")})


def lot_movement_with_error(request, pk, msg):
    from .vessels import vessel_options
    from .blend import blend_source_lots
    from cellar.models import ExternalDestination
    from cellar.services import volumes as vol_svc
    lot = get_object_or_404(Lot, pk=pk)
    return _inject_error(request, pk, "web/_lot_movement.html", None, msg, lotpages.movements, "rows",
                         {"vessel_options": vessel_options(exclude_lot=lot),
                          "blend_sources": blend_source_lots(exclude_lot=lot),
                          "external_destinations": ExternalDestination.objects.order_by("name"),
                          "current_balance": vol_svc.lot_balance(lot),
                          "today": timezone.localdate().isoformat(),
                          "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M")})


def _inject_error(request, pk, template, _resp, msg, builder, key, extra):
    lot = get_object_or_404(Lot, pk=pk)
    data, _ = _safe(builder, lot)
    ctx = {"lot": lot, "section": template.split("_lot_")[1].split(".")[0],
           "note": lotpages.section_note(lot, template.split("_lot_")[1].split(".")[0]),
           key: data or [], "form_error": msg}
    ctx.update(extra)
    return render(request, template, ctx)


# ------------------------------------------------------------------ reports --
# key -> (label, needs)  where needs drives which inputs are required
REPORTS = {
    "5120-17":    ("TTB 5120.17 — Part I", "year_month"),
    "5120-17-p3": ("TTB 5120.17 — Part III (spirits)", "year_month"),
    "5120-17-p4": ("TTB 5120.17 — Part IV (materials)", "year_month"),
    "excise":     ("CBMA excise (5000.24 wine line)", "year_dates"),
    "crush":      ("CA crush report", "year"),
}


@login_required
def reports_index(request):
    return render(request, "web/reports.html",
                  {"nav": "reports",
                   "reports": [(k, v[0], v[1]) for k, v in REPORTS.items()]})


def _int(request, key):
    raw = (request.POST.get(key) or "").strip()
    return int(raw) if raw else None


@login_required
@require_http_methods(["POST"])
def report_run(request):
    """HTMX fragment: run the chosen report for the given period, swap result in."""
    from datetime import date as _date
    key = request.POST.get("report")
    entry = REPORTS.get(key)
    if entry is None:
        return render(request, "web/_report_result.html",
                      {"error": "Unknown report.", "title": "Report"})
    title, needs = entry

    try:
        year = _int(request, "year")
        if year is None:
            raise ValueError("Year is required.")
        totals = None
        download = None
        download_csv = None
        if needs == "year_month":
            month = _int(request, "month")
            if month is None:
                raise ValueError("Month is required for this report.")
            fn = {"5120-17": reporting_svc.build_5120_17,
                  "5120-17-p3": reporting_svc.build_5120_17_part3,
                  "5120-17-p4": reporting_svc.build_5120_17_part4}[key]
            value = fn(year, month)
            # All three views (Part I / III / IV) render into ONE combined 5120.17
            # PDF — there's no separate per-part PDF endpoint, because the filing
            # itself is one document. Offer the same download regardless of which
            # part the person is looking at.
            download = f"/api/reports/5120-17/pdf/?year={year}&month={month}"
        elif needs == "year_dates":
            start = request.POST.get("start") or ""
            end = request.POST.get("end") or ""
            if not start or not end:
                raise ValueError("Start and end dates are required (YYYY-MM-DD).")
            value = excise_svc.compute_period_excise(
                year, _date.fromisoformat(start), _date.fromisoformat(end))
            serial = (request.POST.get("serial") or "").strip()
            if serial:
                download = (f"/api/reports/5000-24/pdf/?year={year}&start={start}"
                            f"&end={end}&serial={serial}")
        else:  # year only -> crush
            rows = crush_svc.ca_crush_report(year)
            totals = crush_svc.crush_report_totals(rows)
            value = rows
            download = f"/api/reports/crush/pdf/?year={year}"
            download_csv = f"/api/reports/crush/csv/?year={year}"
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_report_result.html", {"error": str(e), "title": title})

    return render(request, "web/_report_result.html",
                  {"title": title, "value": value, "totals": totals,
                   "download": download, "download_csv": download_csv, "report_key": key,
                   "period": {"year": year}})


# ------------------------------------------------ reference CRUD (additives) --
# Reference pattern to replicate for other masters. Additives are editable (not
# append-only), so straight create/update. `unit_cost` is a documented field.
@login_required
def additives(request):
    from .reference import REGISTRY
    return render(request, "web/additives.html",
                  {"nav": "reference",
                   "additives": Additive.objects.order_by("name"),
                   "categories": Additive.Category.choices,
                   "tables": REGISTRY.values()})


@login_required
@require_http_methods(["POST"])
def additive_create(request):
    name = (request.POST.get("name") or "").strip()
    category = (request.POST.get("category") or "").strip()
    unit = (request.POST.get("unit") or "").strip()
    valid_cats = {c for c, _ in Additive.Category.choices}
    if not name or category not in valid_cats or not unit:
        return render(request, "web/_additive_row.html",
                      {"error": "Name, a valid category, and unit are all required.",
                       "additive": None}, status=400)
    obj = Additive(name=name, category=category, unit=unit,
                  crush_addition=(request.POST.get("crush_addition") == "on"))
    _apply_unit_cost(obj, request.POST.get("unit_cost"))
    obj.save()
    return render(request, "web/_additive_row.html", {"additive": obj})


@login_required
@require_http_methods(["POST"])
def additive_update(request, pk):
    obj = get_object_or_404(Additive, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if name:
        obj.name = name
    category = (request.POST.get("category") or "").strip()
    if category in {c for c, _ in Additive.Category.choices}:
        obj.category = category
    unit = (request.POST.get("unit") or "").strip()
    if unit:
        obj.unit = unit
    obj.crush_addition = request.POST.get("crush_addition") == "on"
    _apply_unit_cost(obj, request.POST.get("unit_cost"))
    obj.save()
    return render(request, "web/_additive_row.html", {"additive": obj})


def _apply_unit_cost(obj, raw):
    raw = (raw or "").strip()
    if raw == "":
        return
    try:
        obj.unit_cost = raw  # DecimalField accepts the string
    except Exception:
        pass
