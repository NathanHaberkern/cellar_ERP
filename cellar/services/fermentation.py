"""
Fermentation orchestration — the glue for the lot-page Fermentation module.

Reuses the validated primitives rather than re-deriving anything:
  * nutrition.build_plan  — the Scott Labs plan (Go-Ferm + staged Fermaid O)
  * operations.inoculate  — records the InoculationEvent, books Go-Ferm, sets
                            status FERMENTING, returns the plan
  * operations.record_addition / record_reading / transfer_lot
  * tasks.create_task     — with a payload carrying the planned dose, so
                            completing a Fermaid O task books the real Addition (C4)

Design points locked with Nate:
  * Yeast pitch is always 2 lb/1000 gal — already the D21 / GRE additive rate, so
    the dose is booked through record_addition like any other addition (C2).
  * Fermaid O tasks are created up front with an ESTIMATED due date from a
    configurable Brix/day rate, then advanced to "due today" when an actual daily
    Brix reading crosses the trigger (C3 hybrid).
  * Brix + YAN for the plan come from the most recent full juice panel imported
    from ETS (slice A), so the module leans on real lab data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import Lot, ConfigConstant, Reading, LabResult, Task, Additive
from cellar.services import operations as ops
from cellar.services import nutrition
from cellar.services import labpanels
from cellar.services import tasks as tsvc

DEFAULT_BRIX_PER_DAY = 2.5
STRAINS = [("D21", "D21"), ("GRE", "GRE"), ("native", "Native (no yeast)")]


def brix_per_day():
    row = ConfigConstant.objects.filter(key="ferment_brix_per_day").first()
    try:
        return float(row.value) if row else DEFAULT_BRIX_PER_DAY
    except (TypeError, ValueError):
        return DEFAULT_BRIX_PER_DAY


DEFAULT_PRESS_READY_BRIX = 0.0     # dryness target used to estimate a press date
DEFAULT_SETTLING_DAYS = 2          # gap between press and barrel-down, before a
                                    # real PressingEvent/settling reading exists


def _config_float(key, default):
    row = ConfigConstant.objects.filter(key=key).first()
    try:
        return float(row.value) if row else default
    except (TypeError, ValueError):
        return default


def estimate_press_and_barrel_dates(lot, asof=None):
    """Rough estimated press and barrel-down dates from the live Brix trend.

    Unlike `_estimate_due` (which projects forward from the INITIAL brix at
    inoculation, for scheduling nutrient additions), this projects forward
    from the MOST RECENT actual reading — so the estimate tightens as real
    readings come in, rather than staying pinned to day-one assumptions.

    Needs at least two Brix readings to compute a rate; with only one (or
    none), returns an estimate of None rather than guessing off a single
    point or the global default rate, which would be more misleading than
    useful this early in the ferment.

    Returns {'press_date': date|None, 'barrel_down_date': date|None,
             'basis': str} — `basis` explains what the estimate is built on
    (or why there isn't one yet), meant to be shown next to the number so it
    always reads as an estimate, never a commitment.
    """
    asof = asof or timezone.localdate()
    readings = list(Reading.objects.filter(
        lot=lot, analyte=Reading.Analyte.BRIX, voided_at__isnull=True
    ).order_by("measured_at"))

    if len(readings) < 2:
        basis = "need at least two Brix readings to project a trend"
        return {"press_date": None, "barrel_down_date": None, "basis": basis}

    latest = readings[-1]
    prior = readings[-2]
    latest_date = timezone.localtime(latest.measured_at).date() if hasattr(latest.measured_at, "date") else latest.measured_at
    prior_date = timezone.localtime(prior.measured_at).date() if hasattr(prior.measured_at, "date") else prior.measured_at
    span_days = (latest_date - prior_date).days
    observed_drop = float(prior.value) - float(latest.value)

    if span_days <= 0 or observed_drop <= 0:
        # Flat or rising reading (e.g. a re-check same day, or a stuck/rising
        # ferment) — fall back to the configured average rather than divide
        # by zero or project backwards.
        rate = brix_per_day()
        basis = f"flat/rising trend — using the {rate:g} °Brix/day default rate"
    else:
        rate = observed_drop / span_days
        basis = f"{rate:.2f} °Brix/day, from the last two readings ({prior_date} → {latest_date})"

    target = _config_float("press_ready_brix", DEFAULT_PRESS_READY_BRIX)
    remaining = float(latest.value) - target
    if remaining <= 0:
        press_date = latest_date  # already at/below target
    else:
        days_out = math.ceil(remaining / rate) if rate > 0 else None
        press_date = latest_date + timedelta(days=days_out) if days_out is not None else None

    settling_days = int(_config_float("settling_days_before_barrel", DEFAULT_SETTLING_DAYS))
    barrel_date = (press_date + timedelta(days=settling_days)) if press_date else None

    return {"press_date": press_date, "barrel_down_date": barrel_date, "basis": basis}


def brix_series(lot):
    """Chronological (date, float) Brix readings — the sparkline's data."""
    rows = (Reading.objects.filter(lot=lot, analyte=Reading.Analyte.BRIX,
                                   voided_at__isnull=True)
            .order_by("measured_at"))
    out = []
    for r in rows:
        d = timezone.localtime(r.measured_at).date() if hasattr(r.measured_at, "date") else r.measured_at
        out.append((d, float(r.value)))
    return out


# ----------------------------------------------------------- juice metrics
def juice_metrics(lot):
    """(brix, yan, source) from the most recent full juice panel; falls back to
    the latest Brix reading and latest YAN value if no full panel exists yet."""
    result, _ = labpanels.latest_full_panel(lot)
    brix = yan = None
    source = ""
    if result is not None and result.panel == LabResult.Panel.JUICE:
        for v in result.values.all():
            if v.analyte.slug == "brix":
                brix = float(v.value)
            elif v.analyte.slug == "yan":
                yan = float(v.value)
        source = f"juice panel {result.reported_at:%Y-%m-%d}"

    if brix is None:
        r = (Reading.objects.filter(lot=lot, analyte=Reading.Analyte.BRIX,
                                    voided_at__isnull=True)
             .order_by("-measured_at").first())
        if r:
            brix = float(r.value)
            source = source or f"Brix reading {r.measured_at:%Y-%m-%d}"
    if yan is None:
        from cellar.models import LabResultValue
        v = (LabResultValue.objects.filter(result__lot=lot, analyte__slug="yan",
                                            voided_at__isnull=True)
             .order_by("-result__reported_at").first())
        if v:
            yan = float(v.value)
    return brix, yan, source


# ------------------------------------------------------------ plan preview
@dataclass
class PlanPreview:
    strain: str
    volume_gal: float
    brix: float
    yan: float
    source: str
    yeast_label: str
    yeast_grams: float | None
    plan: object                      # nutrition.NutritionPlan
    staged: list = field(default_factory=list)   # [{stage, dose_g_hl, grams, trigger_brix, due}]
    warnings: list = field(default_factory=list)


def _estimate_due(start_date, initial_brix, trigger_brix, per_day):
    if trigger_brix is None:
        return start_date
    drop = max(0.0, float(initial_brix) - float(trigger_brix))
    days = math.ceil(drop / per_day) if per_day > 0 else 0
    return start_date + timedelta(days=days)


def plan_preview(lot, *, strain, volume_gal, brix, yan, start_date=None):
    """Compute the full Step-1 plan for display — no writes."""
    start_date = start_date or timezone.localdate()
    per_day = brix_per_day()
    native = (strain or "").lower() == "native"

    plan = nutrition.build_plan(initial_brix=float(brix), juice_yan=float(yan),
                                strain=("native" if native else strain),
                                volume_gal=float(volume_gal))

    # yeast dose (2 lb/1000 gal) via the strain additive, unless native
    yeast_label, yeast_grams = "Native ferment (no yeast)", None
    if not native:
        try:
            d = ops.preview_addition(lot, strain, volume_gal=volume_gal)
            yeast_label = d["computed"]
            yeast_grams = float(d["quantity"])
        except Additive.DoesNotExist:
            yeast_label = f"{strain} (additive not seeded)"

    staged = []
    for add in plan.adds:
        if add.trigger_brix is None:
            continue
        staged.append({
            "stage": add.stage, "dose_g_hl": add.dose_g_hl, "grams": add.grams,
            "trigger_brix": add.trigger_brix,
            "due": _estimate_due(start_date, brix, add.trigger_brix, per_day),
            "note": add.note,
        })

    return PlanPreview(
        strain=strain, volume_gal=float(volume_gal), brix=float(brix), yan=float(yan),
        source=juice_metrics(lot)[2], yeast_label=yeast_label, yeast_grams=yeast_grams,
        plan=plan, staged=staged, warnings=list(plan.warnings))


# ------------------------------------------------------- start fermentation
@transaction.atomic
def start_fermentation(lot, *, inoculated_at, strain, volume_gal, brix, yan, actor=None):
    """Step 1 commit: inoculate (books Go-Ferm, sets FERMENTING), book the yeast
    addition, and create the staged Fermaid O tasks with estimated due dates and a
    payload so completing each one books the real Fermaid O addition."""
    native = (strain or "").lower() == "native"
    ev, plan = ops.inoculate(
        lot, inoculated_at=inoculated_at, native=native,
        yeast_strain=None if native else strain,
        volume_gal=volume_gal, initial_brix=brix, juice_yan=yan)

    # yeast addition (2 lb/1000 gal) — Go-Ferm already booked inside inoculate()
    if not native:
        try:
            ops.record_addition(lot, strain, added_at=inoculated_at, volume_gal=volume_gal)
        except Additive.DoesNotExist:
            pass

    start_date = timezone.localdate()
    per_day = brix_per_day()
    made = 0
    for add in plan.adds:
        if add.trigger_brix is None:
            continue
        due = _estimate_due(start_date, brix, add.trigger_brix, per_day)
        _, created = tsvc.create_task(
            title=f"Add Fermaid O — {lot.code} ({add.stage})",
            body=(f"{add.dose_g_hl:g} g/hL ≈ {add.grams:g} g at {add.stage} "
                  f"(≈ {add.trigger_brix:g} °Brix). Mark done to confirm the dose."),
            due_date=due, lot=lot, actor=actor,
            dedupe_key=f"fermaid:{lot.pk}:{start_date.isoformat()}:{add.stage}",
            payload={"additive": add.product, "dose_g_hl": add.dose_g_hl,
                     "grams": add.grams, "trigger_brix": add.trigger_brix,
                     "volume_gal": float(volume_gal), "stage": add.stage})
        made += int(created)
    return ev, plan, made


# ----------------------------------------------------- Fermaid O confirmation
@transaction.atomic
def confirm_fermaid_task(task, *, actual_g_hl=None, added_at=None, actor=None):
    """Complete a Fermaid O task and book the real addition (C4). `actual_g_hl`
    overrides the planned dose; blank uses the plan."""
    p = task.payload or {}
    additive = p.get("additive", "Fermaid O")
    dose = actual_g_hl if actual_g_hl not in (None, "") else p.get("dose_g_hl")
    added_at = added_at or timezone.now()
    ops.record_addition(task.lot, additive, added_at=added_at,
                        volume_gal=p.get("volume_gal"), rate_override=dose)
    tsvc.complete_task(task, actor=actor,
                       detail=f"booked {additive} {dose} g/hL")
    return task


# ------------------------------------------------------ daily Brix handling
def _complete_daily(lot, kind, actor):
    """Auto-complete today's auto-generated ferment task (reading / cap) when the
    matching action is recorded."""
    key = f"{kind}:{lot.pk}:{timezone.localdate().isoformat()}"
    t = Task.objects.filter(dedupe_key=key, status=Task.Status.OPEN).first()
    if t:
        tsvc.complete_task(t, actor=actor, detail="logged")


def on_brix_reading(lot, brix, actor=None):
    """Advance any staged Fermaid O task whose trigger the reading has reached, so
    it surfaces as due today rather than on its estimated date (C3)."""
    advanced = 0
    for t in Task.objects.filter(lot=lot, status=Task.Status.OPEN):
        p = t.payload or {}
        trig = p.get("trigger_brix")
        if trig is None or p.get("advanced"):
            continue
        if float(brix) <= float(trig):
            t.due_date = timezone.localdate()
            p["advanced"] = True
            t.payload = p
            t.save(update_fields=["due_date", "payload"])
            from cellar.models import TaskEvent
            TaskEvent.objects.create(task=t, kind=TaskEvent.Kind.EDITED,
                                     detail=f"trigger reached (Brix {brix})",
                                     operator=actor if getattr(actor, "is_authenticated", False) else None)
            advanced += 1
    return advanced


@transaction.atomic
def record_daily(lot, *, brix=None, temp=None, measured_at=None, cap=None, actor=None):
    """Step 2: record today's Brix + temp and a cap event; append to the ledger and
    close the matching daily tasks. `cap` is 'pumpover' | 'punchdown' | None."""
    from cellar.models import PumpOverEvent, PunchDownEvent
    measured_at = measured_at or timezone.now()
    if brix not in (None, ""):
        ops.record_reading(lot, analyte=Reading.Analyte.BRIX, value=brix, measured_at=measured_at)
        on_brix_reading(lot, brix, actor=actor)
        _complete_daily(lot, "fermread", actor)
    if temp not in (None, ""):
        ops.record_reading(lot, analyte=Reading.Analyte.TEMP, value=temp, measured_at=measured_at)
    if cap == "pumpover":
        PumpOverEvent.objects.create(lot=lot, started_at=measured_at)
        _complete_daily(lot, "fermcap", actor)
    elif cap == "punchdown":
        PunchDownEvent.objects.create(lot=lot, occurred_at=measured_at)
        _complete_daily(lot, "fermcap", actor)


# --------------------------------------------------------------- Step 3 / 4
@transaction.atomic
def press_to_vessel(lot, *, vessel, volume_gal, at=None, allow_blend=False, actor=None):
    """Step 3: press the lot to a new vessel, gauge it, and RECORD THE PRESS.

    This used to move the wine and state a volume without writing a PressingEvent —
    so the press itself left no trace, and press_yield_estimate() could never learn a
    varietal average because there was nothing to learn from. Reds press after
    fermentation and go straight on (disposition=TO_BARREL, no settling), so the
    press gauge is the booking volume: this is the number book_to_bond() will read.

    `allow_blend` co-occupies an already-occupied tank (same semantics as a Movement
    transfer) instead of raising "SS-1 is occupied by ...".
    """
    from cellar.models import PressingEvent
    from cellar.services import pressing

    at = at or timezone.now()
    if volume_gal in (None, ""):
        # no gauge — fall back to the old behaviour rather than book a phantom press
        ops.transfer_lot(lot, vessel, at, allow_blend=allow_blend)
        lot.status = Lot.Status.PRESSED
        lot.save(update_fields=["status"])
        return None

    return pressing.press(
        lot, pressed_at=at, total_gal=volume_gal, to_vessel=vessel,
        settling_days=None, disposition=PressingEvent.Disposition.TO_BARREL,
        is_booking_volume=True, allow_blend=allow_blend, actor=actor)


def empty_oak_containers():
    from cellar.models import Container, AgingPlacement
    open_ids = set(
        AgingPlacement.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("container_id", flat=True))
    return (Container.objects
            .filter(active=True, type__in=[Container.Type.BARREL, Container.Type.FOUDRE])
            .exclude(id__in=open_ids)
            .order_by("container_id"))


@transaction.atomic
def rack_to_barrel(lot, *, container_ids, total_volume_gal, filled_at=None, actor=None):
    """Step 4: rack the lot to barrels, close its tank assignment, and flip status
    to DONE_PRIMARY — which hides the Fermentation module and restores Additions."""
    from cellar.models import Container, AgingPlacement, TankAssignment
    filled_at = filled_at or timezone.localdate()
    ids = [int(c) for c in container_ids if str(c).strip()]
    if not ids:
        raise ValueError("Select at least one barrel to rack into.")
    per = Decimal(str(total_volume_gal)) / Decimal(len(ids)) if total_volume_gal else Decimal("0")

    for cid in ids:
        container = Container.objects.get(pk=cid)
        AgingPlacement.objects.create(lot=lot, container=container,
                                      filled_at=filled_at,
                                      volume_gal=per.quantize(Decimal("0.1")))
    # close any open tank assignment
    (TankAssignment.objects.filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
     .update(emptied_at=timezone.now()))

    # The barrel-down. Fermentation is over, so this is the first gauge of WINE — the
    # production figure for Part I line 2. Recorded as the booking volume so
    # bond.book_to_bond() has something authoritative to read; nothing is booked until
    # a human confirms it.
    if total_volume_gal not in (None, ""):
        from cellar.models import VolumeMeasurement
        VolumeMeasurement.objects.create(
            lot=lot, method=VolumeMeasurement.Method.BARREL_BACKFILL,
            measured_at=timezone.now(),
            volume_gal=Decimal(str(total_volume_gal)).quantize(Decimal("0.1")),
            barrels_filled=len(ids), is_booking_volume=True)

    lot.status = Lot.Status.DONE_PRIMARY
    lot.save(update_fields=["status"])
