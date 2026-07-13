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
DEFAULT_MIN_SKIN_CONTACT_DAYS = 10 # red-wine mandatory floor (Nate's call, Jul 2026)
LOGIT_EPS = 1e-3                   # keeps logit() finite near f=0/1
MIN_LOGISTIC_READINGS = 3          # below this, fall back to the two-point rate


def _config_float(key, default):
    row = ConfigConstant.objects.filter(key=key).first()
    try:
        return float(row.value) if row else default
    except (TypeError, ValueError):
        return default


def _as_date(value):
    return timezone.localtime(value).date() if hasattr(value, "hour") else value


# ------------------------------------------------------ fermentation kinetics
def _logit(f):
    f = min(max(f, LOGIT_EPS), 1 - LOGIT_EPS)
    return math.log(f / (1 - f))


def _fit_logistic_days_remaining(series, target):
    """Project days-from-last-reading to `target` Brix using a logistic
    (S-curve) fit instead of a straight line.

    WHY: real fermentation isn't linear — slow start, fast middle third, long
    decelerating tail as sugar depletes and alcohol/temperature stress the
    yeast. A straight-line projection from two points is consistently wrong
    in the tail, which is exactly when an accurate "how much longer" matters
    most. Cumulative fraction-fermented f(t) is well approximated by a
    logistic curve, and a logistic curve is LINEAR after a logit transform:
    logit(f) = ln(f/(1-f)) = a + b·t. That means an ordinary least-squares
    fit — the same tool the old two-point method already used — captures the
    S-curve shape once it's fit in transformed space, no new dependency
    needed.

    series[0] anchors f=0 (the starting sugar level, S0) and is not itself a
    fit point (logit(0) is undefined) — the fit runs on the remaining
    readings, so this needs >= 3 total readings (1 anchor + >= 2 fit points).

    Returns (days_remaining, note) — note explains the fit or, on failure,
    why it fell back. days_remaining can be negative (already past target).
    """
    s0 = series[0][1]
    t0 = series[0][0]
    span = s0 - target
    if span <= 0:
        return None, "initial Brix was already at/below the press-ready target"

    pts = []
    for d, v in series[1:]:
        t_days = (d - t0).days
        if t_days <= 0:
            continue  # same-day re-check as the anchor; not a usable fit point
        f = (s0 - v) / span
        pts.append((t_days, _logit(f)))
    if len(pts) < 2:
        return None, "not enough post-anchor readings for a logistic fit"

    n = len(pts)
    mean_t = sum(p[0] for p in pts) / n
    mean_y = sum(p[1] for p in pts) / n
    num = sum((t - mean_t) * (y - mean_y) for t, y in pts)
    den = sum((t - mean_t) ** 2 for t, y in pts)
    if den == 0:
        return None, "all usable readings fall on the same day"
    b = num / den
    a = mean_y - b * mean_t
    if b <= 0:
        return None, "fitted trend is flat or rising — logistic fit not usable"

    # Time at which the fitted curve crosses "practically at target" (f
    # clamped to 1-LOGIT_EPS, since f=1 exactly is asymptotic).
    t_target = (_logit(1 - LOGIT_EPS) - a) / b
    last_t = (series[-1][0] - t0).days
    days_remaining = t_target - last_t
    return days_remaining, f"logistic fit (S-curve, {n} readings past the {s0:g} °Brix start)"


def estimate_press_and_barrel_dates(lot, asof=None):
    """Estimated press and barrel-down dates.

    Two independent inputs, combined by taking the LATER date:
      1. A kinetics estimate from the live Brix trend — logistic fit with 3+
         readings (see `_fit_logistic_days_remaining`), the simple observed
         two-point rate with exactly 2, or nothing with fewer than 2.
      2. A mandatory minimum skin-contact floor for red wine on the skin (see
         `skin_contact_floor_date`) — a wine can always be held longer than
         the kinetics alone would suggest, so the floor wins when it's later.
         This never blocks pressing early in the real workflow; it only
         floors the *estimate* shown here.

    Returns {'press_date', 'barrel_down_date', 'basis'} — `basis` always
    explains what produced the number, so it reads as a planning estimate to
    verify, never a commitment.
    """
    asof = asof or timezone.localdate()
    target = _config_float("press_ready_brix", DEFAULT_PRESS_READY_BRIX)
    series = brix_series(lot)  # ascending [(date, brix), ...]

    kinetics_date = None
    basis_parts = []
    simple_rate = None
    latest_date = latest_val = None

    if len(series) >= 2:
        latest_date, latest_val = series[-1]
        prior_date, prior_val = series[-2]
        span_days = (latest_date - prior_date).days
        observed_drop = prior_val - latest_val
        if span_days > 0 and observed_drop > 0:
            simple_rate = observed_drop / span_days

    if len(series) >= MIN_LOGISTIC_READINGS and simple_rate:
        days_remaining, note = _fit_logistic_days_remaining(series, target)
        if days_remaining is not None:
            # Sanity-bound the fit against the plain observed rate so one
            # noisy early reading can't swing the projection wildly — a
            # transform-shaped fit is only worth trusting within shouting
            # distance of what the raw data plainly shows.
            simple_days = (latest_val - target) / simple_rate if simple_rate else None
            if simple_days and simple_days > 0:
                lo, hi = 0.2 * simple_days, 4 * simple_days
                if not (lo <= days_remaining <= hi):
                    days_remaining = None
                    note = "logistic projection was out of a plausible range vs. the observed rate — falling back"
            if days_remaining is not None:
                kinetics_date = latest_date + timedelta(days=max(0, math.ceil(days_remaining)))
                basis_parts.append(note)

    if kinetics_date is None and len(series) >= 2:
        if simple_rate:
            remaining = latest_val - target
            days_out = math.ceil(remaining / simple_rate) if remaining > 0 else 0
            kinetics_date = latest_date + timedelta(days=days_out)
            basis_parts.append(f"{simple_rate:.2f} °Brix/day, from the last two readings "
                                f"({prior_date} → {latest_date})")
        else:
            rate = brix_per_day()
            remaining = latest_val - target
            days_out = math.ceil(remaining / rate) if (rate > 0 and remaining > 0) else 0
            kinetics_date = latest_date + timedelta(days=days_out)
            basis_parts.append(f"flat/rising trend — using the {rate:g} °Brix/day default rate")

    if len(series) < 2:
        basis_parts.append("need at least two Brix readings to project a trend")

    floor_date, floor_days = skin_contact_floor_date(lot)
    press_date = kinetics_date
    if floor_date is not None and (press_date is None or floor_date > press_date):
        if press_date is not None:
            basis_parts.append(f"floored to the {floor_days}-day minimum skin contact "
                                f"(kinetics alone suggested pressing earlier)")
        else:
            basis_parts.append(f"{floor_days}-day minimum skin contact — no Brix trend yet")
        press_date = floor_date

    settling_days = int(_config_float("settling_days_before_barrel", DEFAULT_SETTLING_DAYS))
    barrel_date = (press_date + timedelta(days=settling_days)) if press_date else None

    return {"press_date": press_date, "barrel_down_date": barrel_date,
            "basis": "; ".join(basis_parts) if basis_parts else "—"}


# ---------------------------------------------------- red skin-contact floor
# Only genuine skin-contact red paths — NOT rosé (Path B, deliberately short
# skin contact by style) and NOT direct-press (Path C). Whole-cluster red
# (E) macerates same as destemmed red (D), so both count.
RED_SKIN_CONTACT_PATHS = ("D", "E")


def min_skin_contact_days(lot):
    """Effective minimum, per-lot override if set, else the winery default."""
    override = getattr(lot, "fermentation_override", None)
    if override is not None and override.min_skin_contact_days is not None:
        return override.min_skin_contact_days
    return int(_config_float("min_skin_contact_days_red", DEFAULT_MIN_SKIN_CONTACT_DAYS))


def skin_contact_floor_date(lot):
    """(floor_date, days) if this lot is on a red skin-contact path and has a
    recorded destem date, else (None, None). This is a planning floor on the
    ESTIMATE only — it never blocks a real Press action if you choose to
    press earlier for your own reasons; that stays entirely up to you.
    """
    destem = (lot.destemmings.filter(voided_at__isnull=True)
              .order_by("destem_at").first())
    if destem is None or destem.processing_path not in RED_SKIN_CONTACT_PATHS:
        return None, None
    days = min_skin_contact_days(lot)
    return _as_date(destem.destem_at) + timedelta(days=days), days


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
