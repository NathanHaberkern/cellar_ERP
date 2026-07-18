"""
Fermentation module — front end (HTMX), lives on the lot page.

Renders the right step(s) from the lot's status:
  * not yet inoculated  → Step 1 (yeast + nutrition plan) with a live preview
  * fermenting          → the plan/tasks, Step 2 daily entry, Step 3 press
  * pressed / settling  → Step 4 rack-to-barrel (flips status → done_primary)

The fermentation flow shows/hides steps per the status window; this
module assumes it's only reached inside that window.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Lot, Vessel, Reading, Task
from cellar.services import fermentation as fz
from cellar.services import operations as ops
from cellar.services import pressing as press_svc
from . import lotpages


def _section_note(lot, section):
    return lotpages.section_note(lot, section)

# status windows (drive progressive disclosure in the Fermentation tile)
FERMENT_WINDOW = {Lot.Status.COLD_SOAK, Lot.Status.FERMENTING,
                  Lot.Status.PRESSED, Lot.Status.SETTLING}


def _parse_dt(raw):
    from django.utils.dateparse import parse_datetime
    raw = (raw or "").strip()
    if not raw:
        return timezone.now()
    dt = parse_datetime(raw)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt or timezone.now()


def ferment_ctx(lot):
    inoculated = lot.inoculations.filter(voided_at__isnull=True).exists()
    status = lot.status
    press_first = press_svc.presses_first(lot)

    if press_first:
        # White/rosé: intake → PROCESSING (needs press) → SETTLING (needs rack off
        # gross lees) → still SETTLING once clear (needs inoculate) → FERMENTING.
        # Nothing here ends primary — booking happens off the barrel-down/tank
        # gauge later, same as red, via the summary card.
        already_pressed = lot.pressings.filter(voided_at__isnull=True).exists()
        show_press_first = (not already_pressed) and status in (
            Lot.Status.PROCESSING, Lot.Status.RECEIVING)
        show_rack_lees = already_pressed and status == Lot.Status.SETTLING and (
            Task.objects.filter(lot=lot, status=Task.Status.OPEN,
                                dedupe_key__startswith=f"grosslees:{lot.pk}:").exists())
        show_inoculate = (not inoculated) and already_pressed and not show_rack_lees \
            and status not in (Lot.Status.DONE_PRIMARY, Lot.Status.PLANNED)
        show_daily = inoculated and status == Lot.Status.FERMENTING
        show_press = False   # the red-style Step 3 form never applies here
        show_rack = False    # rack-to-barrel lives on the Oak tab regardless
    else:
        show_press_first = False
        show_rack_lees = False
        show_inoculate = (not inoculated) and status not in (Lot.Status.DONE_PRIMARY, Lot.Status.PLANNED)
        show_daily = inoculated and status in (Lot.Status.FERMENTING, Lot.Status.COLD_SOAK)
        show_press = inoculated and status in (Lot.Status.FERMENTING, Lot.Status.COLD_SOAK)
        show_rack = status in (Lot.Status.PRESSED, Lot.Status.SETTLING)

    ctx = {
        "lot": lot,
        "section": "fermentation",
        "note": _section_note(lot, "fermentation"),
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "press_first": press_first,
        "show_press_first": show_press_first,
        "show_rack_lees": show_rack_lees,
        "show_inoculate": show_inoculate,
        "show_daily": show_daily,
        "show_press": show_press,
        "show_rack": show_rack,
    }

    if show_press_first or show_press or show_rack_lees:
        from .vessels import vessel_options
        ctx["vessel_options"] = vessel_options(exclude_lot=lot)

    if show_press_first:
        ctx["est_volume"] = ops.current_volume(lot)

    if show_rack_lees:
        prior = lot.pressings.filter(voided_at__isnull=True).order_by("-pressed_at").first()
        ctx["press_gauge"] = prior.volume.volume_gal if (prior and prior.volume) else None

    if show_inoculate:
        brix, yan, source = fz.juice_metrics(lot)
        ctx.update({
            "strains": fz.STRAINS,
            "volume": ops.current_volume(lot),
            "brix": brix, "yan": yan, "metric_source": source,
        })

    if inoculated:
        # staged Fermaid O tasks (the nutrition plan as live tasks)
        ctx["fermaid_tasks"] = (Task.objects.filter(lot=lot, status=Task.Status.OPEN)
                                .exclude(payload={}).order_by("due_date"))
        ctx["readings"] = (Reading.objects.filter(lot=lot, voided_at__isnull=True)
                           .order_by("-measured_at")[:6])

    if show_rack:
        ctx["barrels"] = fz.empty_oak_containers()

    # Red skin-contact minimum override — moved here from the legacy summary card.
    # Only surfaced on an actual skin-contact red path.
    floor_date, floor_days = fz.skin_contact_floor_date(lot)
    if floor_days is not None:
        ctx["skin_contact"] = {
            "days": floor_days, "floor_date": floor_date,
            "override": getattr(lot, "fermentation_override", None),
        }

    return ctx


@login_required
def lot_ferment(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/_lot_ferment.html", ferment_ctx(lot))


def _panel(request, lot, error=None):
    ctx = ferment_ctx(lot)
    if error:
        ctx["error"] = error
    return render(request, "web/_lot_ferment.html", ctx)


@login_required
@require_http_methods(["POST"])
def ferment_preview(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        pp = fz.plan_preview(
            lot, strain=request.POST.get("strain") or "D21",
            volume_gal=float(request.POST.get("volume") or 0),
            brix=float(request.POST.get("brix") or 0),
            yan=float(request.POST.get("yan") or 0))
        return render(request, "web/_ferment_plan.html", {"pp": pp, "lot": lot})
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_ferment_plan.html", {"error": str(e), "lot": lot})


@login_required
@require_http_methods(["POST"])
def ferment_inoculate(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        fz.start_fermentation(
            lot, inoculated_at=_parse_dt(request.POST.get("inoculated_at")),
            strain=request.POST.get("strain") or "D21",
            volume_gal=float(request.POST.get("volume") or 0),
            brix=float(request.POST.get("brix") or 0),
            yan=float(request.POST.get("yan") or 0),
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_daily(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        fz.record_daily(
            lot, brix=request.POST.get("brix"), temp=request.POST.get("temp"),
            cap=request.POST.get("cap") or None,
            measured_at=_parse_dt(request.POST.get("measured_at")),
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_confirm(request, pk, task_pk):
    lot = get_object_or_404(Lot, pk=pk)
    task = get_object_or_404(Task, pk=task_pk, lot=lot)
    try:
        fz.confirm_fermaid_task(
            task, actual_g_hl=request.POST.get("dose") or None,
            added_at=_parse_dt(request.POST.get("added_at")), actor=request.user)
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_press_first(request, pk):
    """White/rosé Step: press to a vessel BEFORE fermentation. Not the booking
    volume — juice isn't wine yet. Opens the settling task via pressing.press()."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        vessel = get_object_or_404(Vessel, pk=request.POST.get("vessel"))
        from cellar.services import pressing
        pressing.press(
            lot, pressed_at=_parse_dt(request.POST.get("pressed_at")),
            total_gal=request.POST.get("volume"), to_vessel=vessel,
            disposition=pressing.PressingEvent.Disposition.GROSS_LEES,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_rack_lees(request, pk):
    """White/rosé Step: rack the settled juice off its gross lees. Closes the
    settling task and leaves the lot in SETTLING, ready to inoculate."""
    lot = get_object_or_404(Lot, pk=pk)
    try:
        from cellar.services import pressing
        vessel_pk = request.POST.get("vessel") or None
        vessel = Vessel.objects.get(pk=vessel_pk) if vessel_pk else None
        pressing.rack_off_gross_lees(
            lot, racked_at=_parse_dt(request.POST.get("racked_at")),
            clear_gal=request.POST.get("clear_gal"), to_vessel=vessel,
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_press(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        vessel = get_object_or_404(Vessel, pk=request.POST.get("vessel"))
        fz.press_to_vessel(lot, vessel=vessel,
                           volume_gal=request.POST.get("volume"),
                           at=_parse_dt(request.POST.get("pressed_at")),
                           actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    return _panel(request, lot)


@login_required
@require_http_methods(["POST"])
def ferment_rack(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    try:
        fz.rack_to_barrel(
            lot, container_ids=request.POST.getlist("containers"),
            total_volume_gal=request.POST.get("volume"),
            filled_at=parse_date(request.POST.get("filled_at") or "") or timezone.localdate(),
            actor=request.user)
        lot.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        return _panel(request, lot, error=str(e))
    # after racking, the module hides — tell the page to reload so the tabs update
    resp = _panel(request, lot)
    resp["HX-Refresh"] = "true"
    return resp
