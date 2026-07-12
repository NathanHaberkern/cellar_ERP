"""
Cellar operations service — the write/orchestration layer the HTMX front end
calls directly (same pattern as the read-side services; never via the JSON API).

This module is the intake half of the rework: receiving fruit through
inoculation. Each function READS current lot/vessel state, applies the domain
math, and writes append-only rows in one transaction — so the UI only ever
prompts for the few real decisions and lets the system compute the rest.

Reconciled design decisions (from the SOP + Nate):
  * One lot may span several bins; per-bin readings/additions carry a vessel FK.
    Bins have no durable IDs — they're created per-lot, labelled A/B/C.
  * Intake volume estimate = tons x 170 (red paths D/E) or x 160 (white/rosé
    paths A/B/C/F).  Press-yield estimate = historic varietal avg, else 165/T.
  * The running volume is persisted as a STATED VolumeMeasurement so additions
    can read it back; measured gauges (rack/book) supersede it by recency for
    ops and by confidence for compliance.
  * Path D (red destemmed) auto-schedules inoculation ~2 working days out.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from cellar.models import (
    Lot, Vessel, Additive, WeighTag, WeighTagBin, WeighTagAllocation,
    DestemmingEvent, TankAssignment, ColdSoakSchedule, InoculationEvent,
    Reading, Addition, VolumeMeasurement,
)
from cellar.services import generator
from cellar.services import nutrition

# ---- unit constants -------------------------------------------------------
GAL_PER_HL = Decimal("26.417205")
G_PER_LB = Decimal("453.59237")
KMBS_SO2_FRACTION = Decimal("0.5764")     # K2S2O5 → 2·SO2 / MW
L_PER_GAL = Decimal("3.785411784")

RED_PATHS = {DestemmingEvent.Path.D, DestemmingEvent.Path.E}
# Whites and rosés press before fermentation; reds ferment on skins and press after.
PRESS_FIRST_PATHS = {DestemmingEvent.Path.A, DestemmingEvent.Path.B,
                     DestemmingEvent.Path.C, DestemmingEvent.Path.F}
DEFAULT_SETTLING_DAYS = 2
INTAKE_GAL_PER_TON_RED = Decimal("170")
INTAKE_GAL_PER_TON_WHITE = Decimal("160")
PRESS_YIELD_FALLBACK = Decimal("165")


# ======================================================================
# estimates & current state
# ======================================================================
def tons_from_lbs(net_lbs) -> Decimal:
    return (Decimal(net_lbs) / Decimal("2000")).quantize(Decimal("0.01"))


def open_assignment_for(vessel):
    """The vessel's current open (unvacated) assignment, or None."""
    return (TankAssignment.objects
            .filter(vessel=vessel, voided_at__isnull=True, emptied_at__isnull=True)
            .select_related("lot").order_by("-assigned_at").first())


def assign_lot_to_vessel(lot, vessel, at, *, allow_blend=False):
    """Assign a lot to a vessel, enforcing one lot per vessel — UNLESS this is a
    deliberate blend (allow_blend=True), which permits co-occupancy:
      • scenario A — two lots racked into the same (new) tank together, and
      • scenario B — a lot racked into a tank another lot already occupies.
    Fresh-fruit intake never blends, so it calls this with allow_blend=False and
    an occupied tank is rejected. Returns the TankAssignment."""
    occ = open_assignment_for(vessel)
    if occ and occ.lot_id != lot.id and not allow_blend:
        raise ValueError(
            f"{vessel.code} is occupied by {occ.lot.code}. Empty it first, "
            f"or record this as a blend to co-occupy.")
    return TankAssignment.objects.create(lot=lot, vessel=vessel, assigned_at=at)


def transfer_lot(lot, to_vessel, at, *, allow_blend=False):
    """Book a tank move for a lot: close its open tank assignment(s) by stamping
    emptied_at (a CLOSE_FIELD), then open a new assignment on `to_vessel`. This is
    how a lot's location changes — there's no separate TankTransfer row. Returns
    the new TankAssignment; raises if the destination is occupied (unless blend)."""
    (TankAssignment.objects
     .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
     .update(emptied_at=at))
    return assign_lot_to_vessel(lot, to_vessel, at, allow_blend=allow_blend)


def generate_weigh_tag_number(block, harvest_date) -> str:
    """Human-readable tag id: '<block>-MMDDYY' (e.g. block 422 picked 09/12/26 →
    '422-091226'). If that exact id is already taken (same block picked again the
    same day), suffix -A, -B, … so it stays unique and readable."""
    base = f"{block.name}-{harvest_date.strftime('%m%d%y')}"
    if not WeighTag.objects.filter(weigh_tag_number=base).exists():
        return base
    for i in range(26):
        cand = f"{base}-{chr(ord('A') + i)}"
        if not WeighTag.objects.filter(weigh_tag_number=cand).exists():
            return cand
    # extraordinarily unlikely; fall back to a numeric tail
    n = 1
    while WeighTag.objects.filter(weigh_tag_number=f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def intake_volume_estimate(net_lbs, path) -> Decimal:
    factor = INTAKE_GAL_PER_TON_RED if path in RED_PATHS else INTAKE_GAL_PER_TON_WHITE
    return (tons_from_lbs(net_lbs) * factor).quantize(Decimal("1"))


def historic_press_yield(variety, min_lots=2):
    """Gal/ton actually achieved on prior bookings of this variety, or None.

    Now answerable: a booked lot has a BookToBond (or an initial FortificationEvent,
    which is the same thing for Port) and weigh-tag allocations, so gallons ÷ tons is
    a real observation. Needs at least `min_lots` before it is worth trusting over the
    SOP number — one odd lot should not move the estimate.
    """
    from cellar.models import BookToBond
    from cellar.services import lotmeta

    gal = Decimal("0")
    tons = Decimal("0")
    n = 0
    for bb in (BookToBond.objects.filter(voided_at__isnull=True)
               .select_related("lot__current_designation")):
        if bb.gallons_produced is None:
            continue
        if lotmeta.lot_variety(bb.lot) != variety:
            continue
        t = sum((a.allocated_net_lbs for a in bb.lot.allocations.filter(voided_at__isnull=True)),
                Decimal("0")) / 2000
        if t <= 0:
            continue
        gal += Decimal(str(bb.gallons_produced))
        tons += t
        n += 1
    if n < min_lots or tons <= 0:
        return None
    return (gal / tons).quantize(Decimal("0.1"))


def press_yield_estimate(net_lbs, variety=None) -> Decimal:
    """Historic varietal average if we have prior bookings for this variety,
    else the SOP fallback of 165 gal/ton."""
    rate = None
    if variety is not None:
        rate = historic_press_yield(variety)
    rate = rate or PRESS_YIELD_FALLBACK
    return (tons_from_lbs(net_lbs) * rate).quantize(Decimal("1"))


def current_volume(lot) -> Decimal | None:
    """Most recent non-void measured/estimated volume — what's in the vessel now."""
    vm = (VolumeMeasurement.objects.filter(lot=lot, voided_at__isnull=True)
          .order_by("-measured_at", "-id").first())
    return vm.volume_gal if vm else None


def _record_volume(lot, gallons, at, method=VolumeMeasurement.Method.STATED, booking=False):
    return VolumeMeasurement.objects.create(
        lot=lot, method=method, measured_at=at,
        volume_gal=Decimal(gallons).quantize(Decimal("0.1")), is_booking_volume=booking)


# ======================================================================
# SO2 / KMBS
# ======================================================================
def so2_kmbs_grams(target_ppm, volume_gal, so2_fraction=KMBS_SO2_FRACTION,
                   current_ppm=0) -> Decimal:
    """Grams of KMBS to raise a volume by (target_ppm - current_ppm)."""
    delta = Decimal(target_ppm) - Decimal(current_ppm)
    litres = Decimal(volume_gal) * L_PER_GAL
    so2_g = delta * litres / Decimal("1000")            # ppm = mg/L → g
    return (so2_g / Decimal(so2_fraction)).quantize(Decimal("0.1"))


# ======================================================================
# working-day helper (cold-soak → inoculation scheduling)
# ======================================================================
def add_working_days(d: date, n: int) -> date:
    cur = d
    while n > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:      # Mon–Fri
            n -= 1
    return cur


# ======================================================================
# RECEIVE + DESTEM  (the "Receiving Fruit" session, one transaction)
# ======================================================================
@transaction.atomic
def receive_and_destem(*, vintage, variety, program, path, destem_at,
                       allocations=None, bin_ids=None, block=None, vineyard=None,
                       tank_code=None, bins=None,
                       crusher_enabled=True, fruit_condition=None, foot_tread=False,
                       foot_tread_pct=None,
                       initial_temp_f=None, hold_hours=None,
                       mog_severity="none", additions=None,
                       cold_soak_days=2, production_intent=""):
    """Create the lot + code, allocate weigh-tag pounds, record the destemming
    event, assign it to a tank OR to freshly-created A/B/C bins, persist the
    intake volume estimate, and (path D) schedule inoculation.

    allocations: list of (weigh_tag, net_lbs) — net-only (purchased) tags.
    bin_ids:     list of WeighTagBin pks to assign to this lot (estate tags with
                 bin lines). Each bin's net rolls up to a per-tag allocation, so a
                 lot may pull bins from several tags and a tag's bins may go to
                 several lots. allocations and bin_ids can be combined.
    bins: list of size-in-tons (e.g. [1, 1, 0.5]) creates bins A, B, C.
    Returns a dict summary for the UI.
    """
    if fruit_condition is None:
        fruit_condition = (DestemmingEvent.Fruit.WHOLE_CLUSTER
                           if path in {DestemmingEvent.Path.E, DestemmingEvent.Path.C,
                                       DestemmingEvent.Path.F}
                           else DestemmingEvent.Fruit.DESTEMMED)

    lot = generator.create_lot(vintage, variety, program,
                               block=block, vineyard=vineyard,
                               status=Lot.Status.PROCESSING,
                               production_intent=production_intent)

    total_lbs = Decimal("0")

    # Bin-level assignment: mark each chosen bin's lot, then roll the bins up to
    # one WeighTagAllocation per tag (keeps tons/costing/remaining-lbs working).
    if bin_ids:
        chosen = list(WeighTagBin.objects.filter(pk__in=bin_ids).select_related("weigh_tag", "assigned_lot"))
        already = [b for b in chosen if b.assigned_lot_id]
        if already:
            raise ValueError(
                "These bins are already assigned: "
                + ", ".join(f"{b.bin_label} → {b.assigned_lot.code}" for b in already)
                + ". Each bin line can only feed one lot.")
        by_tag = {}
        for b in chosen:
            b.assigned_lot = lot
            b.save(update_fields=["assigned_lot"])
            lbs = Decimal(b.net_lbs or 0)
            by_tag[b.weigh_tag] = by_tag.get(b.weigh_tag, Decimal("0")) + lbs
            total_lbs += lbs
        for wt, lbs in by_tag.items():
            WeighTagAllocation.objects.create(weigh_tag=wt, lot=lot, allocated_net_lbs=lbs)

    for wt, lbs in (allocations or []):
        lbs = Decimal(lbs)
        remaining = Decimal(wt.remaining_lbs or 0)
        if lbs > remaining + Decimal("0.01"):
            raise ValueError(
                f"{wt.weigh_tag_number} only has {remaining} lb left "
                f"(requested {lbs}).")
        WeighTagAllocation.objects.create(weigh_tag=wt, lot=lot,
                                          allocated_net_lbs=lbs)
        total_lbs += lbs

    ft_pct = Decimal(str(foot_tread_pct)) if foot_tread_pct is not None else None
    de = DestemmingEvent.objects.create(
        lot=lot, destem_at=destem_at, processing_path=path,
        crusher_enabled=crusher_enabled, fruit_condition=fruit_condition,
        foot_tread=bool(foot_tread or (ft_pct and ft_pct > 0)),
        foot_tread_pct=ft_pct,
        initial_temp_f=initial_temp_f, hold_hours=hold_hours,
        mog_severity=mog_severity or "none",
    )

    vessels = []
    if bins:
        factor = INTAKE_GAL_PER_TON_RED if path in RED_PATHS else INTAKE_GAL_PER_TON_WHITE
        for i, size_tons in enumerate(bins):
            label = chr(ord("A") + i)
            size = Decimal(str(size_tons))
            btype = (Vessel.Type.ONE_TON_BIN if size >= 1
                     else Vessel.Type.MACRO_BIN)
            v = Vessel.objects.create(
                code=f"{lot.code}·{label}", type=btype,
                capacity_gal=(size * factor).quantize(Decimal("1")),
                max_fruit_tons=size,
                refrigerated=(path in RED_PATHS),
                volume_method=Vessel.VolumeMethod.NONE)
            TankAssignment.objects.create(lot=lot, vessel=v, assigned_at=destem_at)
            vessels.append(v)
    elif tank_code:
        tank = Vessel.objects.get(code=tank_code)
        assign_lot_to_vessel(lot, tank, destem_at)   # fresh fruit never blends
        vessels.append(tank)

    est_intake = intake_volume_estimate(total_lbs, path)
    _record_volume(lot, est_intake, destem_at)

    # Crusher additions, recorded in the SAME transaction as the lot so they can be
    # entered up front (append-only means there's no going back to add them later).
    # Doses compute against the just-persisted intake estimate and the lot's tons.
    recorded_additions = []
    for spec in (additions or []):
        add = _resolve_additive(spec["additive"])
        kw = _addition_kwargs(add, spec.get("amount"))
        recorded_additions.append(
            record_addition(lot, add, added_at=destem_at, volume_gal=est_intake, **kw))

    cold_soak = None
    if path == DestemmingEvent.Path.D:
        lot.status = Lot.Status.COLD_SOAK
        lot.save(update_fields=["status"])
        target = add_working_days(destem_at.date(), cold_soak_days + 1)
        cold_soak = ColdSoakSchedule.objects.create(
            lot=lot, start_at=destem_at, target_inoc_date=target)
    elif path in PRESS_FIRST_PATHS:
        # Whites and rosés press BEFORE fermentation. Until now intake created the lot,
        # wrote a 160 gal/ton estimate, and stopped — no press, no settling, no
        # inoculation, and the lot just sat there. Hand it to the press.
        from cellar.services import tasks as _task_svc
        lot.status = Lot.Status.PROCESSING
        lot.save(update_fields=["status"])
        _task_svc.create_task(
            title=f"Press — {lot.code}",
            body=(f"{lot.code} received {destem_at.date()} "
                  f"({tons_from_lbs(total_lbs)} tons, est. {est_intake} gal). "
                  f"Press and gauge what comes off; the press gauge is the booking volume. "
                  f"Settle {DEFAULT_SETTLING_DAYS} days, rack off gross lees, then inoculate."),
            due_date=destem_at.date(), lot=lot,
            dedupe_key=f"press:{lot.pk}")

    return {
        "lot": lot, "code": lot.code, "destemming": de, "vessels": vessels,
        "tons": tons_from_lbs(total_lbs),
        "intake_volume_est_gal": est_intake,
        "press_yield_est_gal": press_yield_estimate(total_lbs, variety),
        "cold_soak": cold_soak,
        "target_inoc_date": cold_soak.target_inoc_date if cold_soak else None,
        "additions": recorded_additions,
    }


# ======================================================================
# ADDITIONS  (computed dose from the additive's default rate / ppm target)
# ======================================================================
def _fmt(d) -> str:
    """Trailing-zero-free decimal for display ('3.5000'→'3.5', '30.0000'→'30')."""
    return f"{Decimal(str(d)).normalize():f}"


def _amount_to_unit(*, grams=None, ml=None, unit) -> Decimal:
    """Express a canonical g/mL amount in the additive's costing unit."""
    u = unit.lower()
    if grams is not None:
        if u == "g":  return grams
        if u == "kg": return grams / Decimal("1000")
        if u == "lb": return grams / G_PER_LB
    if ml is not None:
        if u == "ml": return ml
        if u == "l":  return ml / Decimal("1000")
    return grams if grams is not None else ml


def _compute_dose(additive, *, vol, tons, rate_override=None, target_ppm=None,
                  explicit_quantity=None):
    """Pure dose math (no DB write). Returns dict(target, computed, quantity, basis)."""
    basis = {"volume_gal": str(vol) if vol is not None else None}
    grams = ml = None
    target = ""
    quantity = None

    if additive.dose_mode == Additive.DoseMode.PPM_TARGET:
        ppm = Decimal(str(target_ppm if target_ppm is not None
                          else additive.default_target_ppm))
        frac = additive.so2_fraction or KMBS_SO2_FRACTION
        grams = so2_kmbs_grams(ppm, vol, frac)
        target = f"{_fmt(ppm)} ppm SO₂"
        basis.update(target_ppm=str(ppm), so2_fraction=str(frac))

    elif additive.dose_mode == Additive.DoseMode.PER_TON:
        rate = Decimal(str(rate_override if rate_override is not None else additive.default_rate))
        t = Decimal(tons) if tons is not None else Decimal("0")
        unit_num = additive.rate_unit.split("/")[0].lower()   # 'ml' or 'g'
        amt = rate * t
        if unit_num == "ml":
            ml = amt
        else:
            grams = amt
        target = f"{_fmt(rate)} {additive.rate_unit}"
        basis.update(rate=str(rate), rate_unit=additive.rate_unit, tons=str(t))

    elif additive.dose_mode == Additive.DoseMode.PER_VOLUME:
        rate = Decimal(str(rate_override if rate_override is not None else additive.default_rate))
        num, den = additive.rate_unit.split("/")           # e.g. lb / 1000gal, g / hL
        num, den = num.strip().lower(), den.strip().lower()
        if den in ("1000gal", "1000 gal"):
            base = rate * vol / Decimal("1000")
        elif den == "hl":
            base = rate * (vol / GAL_PER_HL)
        else:
            raise ValueError(f"Unhandled rate denominator: {den!r}")
        if num == "lb":
            grams = base * G_PER_LB
        elif num == "g":
            grams = base
        elif num == "l":
            ml = base * Decimal("1000")
        elif num == "ml":
            ml = base
        else:
            raise ValueError(f"Unhandled rate numerator: {num!r}")
        target = f"{_fmt(rate)} {additive.rate_unit}"
        basis.update(rate=str(rate), rate_unit=additive.rate_unit)

    else:  # BENCH
        if explicit_quantity is None:
            raise ValueError(f"{additive.name} is bench-dosed — enter an explicit quantity.")
        quantity = Decimal(str(explicit_quantity))
        target = "bench trial"

    if quantity is None:
        quantity = _amount_to_unit(grams=grams, ml=ml, unit=additive.unit)
    quantity = Decimal(quantity).quantize(Decimal("0.0001"))
    # dose expressed in the additive's natural/costing unit (lb acid, g nutrient, mL enzyme…)
    computed = f"{_fmt(quantity.quantize(Decimal('0.1')))} {additive.unit} {additive.name}"
    return {"target": target, "computed": computed, "quantity": quantity, "basis": basis}


def _resolve_additive(additive):
    return Additive.objects.get(name=additive) if isinstance(additive, str) else additive


def _addition_kwargs(additive, override):
    """Map one UI override value onto the right dose kwarg for this additive's mode.
    Kept here (not only in the view) so pre-lot previews and the atomic create use
    identical logic."""
    if additive.dose_mode == Additive.DoseMode.PPM_TARGET:
        return {"target_ppm": override}
    if additive.dose_mode == Additive.DoseMode.BENCH:
        return {"explicit_quantity": override}
    return {"rate_override": override}


def preview_addition(lot, additive, *, volume_gal=None, tons=None,
                     rate_override=None, target_ppm=None, explicit_quantity=None):
    """Dry-run: compute the dose for the live UI preview without writing."""
    additive = _resolve_additive(additive)
    vol = Decimal(str(volume_gal)) if volume_gal is not None else current_volume(lot)
    t = Decimal(str(tons)) if tons is not None else _lot_tons(lot)
    return _compute_dose(additive, vol=vol, tons=t, rate_override=rate_override,
                         target_ppm=target_ppm, explicit_quantity=explicit_quantity)


def preview_dose(additive, *, volume_gal, tons, rate_override=None,
                 target_ppm=None, explicit_quantity=None):
    """Lot-less dry-run for the intake form's live preview BEFORE the lot exists —
    computes straight from the intake volume estimate + tons. Same math the atomic
    create will apply, so the previewed dose equals what gets recorded."""
    additive = _resolve_additive(additive)
    vol = Decimal(str(volume_gal)) if volume_gal is not None else None
    t = Decimal(str(tons)) if tons is not None else None
    return _compute_dose(additive, vol=vol, tons=t, rate_override=rate_override,
                         target_ppm=target_ppm, explicit_quantity=explicit_quantity)


@transaction.atomic
def record_addition(lot, additive, *, added_at, volume_gal=None, tons=None,
                    rate_override=None, target_ppm=None, vessel=None,
                    explicit_quantity=None):
    """Compute and record an addition. Reads current lot volume when not given."""
    additive = _resolve_additive(additive)
    vol = Decimal(str(volume_gal)) if volume_gal is not None else current_volume(lot)
    t = Decimal(str(tons)) if tons is not None else _lot_tons(lot)
    d = _compute_dose(additive, vol=vol, tons=t, rate_override=rate_override,
                      target_ppm=target_ppm, explicit_quantity=explicit_quantity)
    return Addition.objects.create(
        lot=lot, vessel=vessel, additive=additive,
        target=d["target"], computed_dose=d["computed"],
        quantity=d["quantity"], basis_snapshot=d["basis"], added_at=added_at)


def _lot_tons(lot) -> Decimal:
    total = sum((a.allocated_net_lbs for a in lot.allocations.filter(voided_at__isnull=True)),
                Decimal("0"))
    return tons_from_lbs(total)


# ======================================================================
# WATER (volume-changing) + READINGS
# ======================================================================
@transaction.atomic
def add_water(lot, *, added_at, pct=None, gallons=None, vessel=None):
    vol = current_volume(lot) or Decimal("0")
    add = Decimal(gallons) if gallons is not None else (vol * Decimal(str(pct)) / 100)
    new_total = vol + add
    _record_volume(lot, new_total, added_at)
    return {"added_gal": add.quantize(Decimal("0.1")), "new_volume_gal": new_total.quantize(Decimal("0.1"))}


def record_reading(lot, *, analyte, value, measured_at, vessel=None, method=""):
    return Reading.objects.create(lot=lot, vessel=vessel, analyte=analyte,
                                  value=Decimal(str(value)), method=method,
                                  measured_at=measured_at)


# ======================================================================
# INOCULATION  (+ Go-Ferm addition + Fermaid O nutrition plan)
# ======================================================================
@transaction.atomic
def inoculate(lot, *, inoculated_at, native=False, yeast_strain=None,
              volume_gal=None, initial_brix, juice_yan):
    """Record inoculation, add Go-Ferm (unless native), and compute the Fermaid O
    plan. Returns (InoculationEvent, NutritionPlan). The staged Fermaid O adds
    come back on the plan for the daily checklist; they're logged as real
    Additions when actually made."""
    vol = Decimal(volume_gal) if volume_gal is not None else current_volume(lot)
    strain_key = "native" if native else (yeast_strain or "native")

    ev = InoculationEvent.objects.create(
        lot=lot, inoculated_at=inoculated_at, native=native,
        yeast_strain="" if native else (yeast_strain or ""),
        goferm=not native)

    plan = nutrition.build_plan(initial_brix=float(initial_brix), juice_yan=float(juice_yan),
                                strain=strain_key, volume_gal=float(vol))

    if not native:
        try:
            record_addition(lot, "Go-Ferm Sterol Flash", added_at=inoculated_at,
                            volume_gal=vol)
        except Additive.DoesNotExist:
            pass

    lot.status = Lot.Status.FERMENTING
    lot.save(update_fields=["status"])
    return ev, plan
