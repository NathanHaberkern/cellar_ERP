"""
Pressing.

`PressingEvent` has existed since the first migration and nothing has ever written
one. `fermentation.press_to_vessel()` moved the wine to a tank and stated a volume;
the press itself — free run, press fraction, settling, the rack off gross lees —
went unrecorded. Two consequences:

  * `press_yield_estimate()` carries a TODO to "derive historic varietal avg from
    prior BookToBond" and can never resolve, because nothing books the press.
  * whites and rosés (paths A, B, C, F) had no path at all. Intake creates the lot,
    estimates a volume at 160 gal/ton, and stops. There is no press, no settling, no
    rack-out, and only path D schedules an inoculation — so a white lot goes into the
    system and then simply sits there.

The white/rosé sequence, per Nate:

    fruit → (destem, unless whole-cluster) → PRESS → juice to tank
          → settle 2 days → rack off gross lees → inoculate

Reds press the other way round — ferment on skins, then press — and that's Step 3 of
the fermentation module, which now writes a PressingEvent too.

The press gauge is the FIRST authoritative volume a white lot has. It is the natural
booking volume, and `bond.book_to_bond()` is built to read it.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (
    DestemmingEvent, Lot, PressingEvent, TankAssignment, VolumeMeasurement,
)
from cellar.services import operations as ops
from cellar.services import tasks as task_svc

GAL = Decimal("0.1")

# Whites and rosés press BEFORE fermentation; reds press after.
PRESS_FIRST_PATHS = {
    DestemmingEvent.Path.A,   # white, destemmed
    DestemmingEvent.Path.B,   # rosé, destemmed
    DestemmingEvent.Path.C,   # rosé, direct press
    DestemmingEvent.Path.F,   # white, whole cluster
}

DEFAULT_SETTLING_DAYS = 2


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v not in (None, "") else None


def presses_first(lot):
    de = lot.destemmings.filter(voided_at__isnull=True).order_by("-destem_at").first() \
        if hasattr(lot, "destemmings") else None
    if de is None:
        from cellar.models import DestemmingEvent as DE
        de = DE.objects.filter(lot=lot, voided_at__isnull=True).order_by("-id").first()
    return bool(de and de.processing_path in PRESS_FIRST_PATHS)


# ======================================================================
# Press
# ======================================================================
@transaction.atomic
def press(lot, *, pressed_at, total_gal, to_vessel=None, free_run_gal=None,
          press_gal=None, settling_days=DEFAULT_SETTLING_DAYS, recombined=True,
          disposition=PressingEvent.Disposition.GROSS_LEES,
          is_booking_volume=None, actor=None):
    """Press a lot and gauge what came off.

    total_gal : what actually came off the press. Free run and press fractions are
                optional detail; Nate recombines and doesn't gauge them separately.

    IS THIS THE PRODUCTION BOOKING? Depends which side of fermentation you're on.

      * A white or rosé presses JUICE. Juice is not wine and has not been produced by
        fermentation — booking 640 gal of Verdelho juice as production would report
        wine that does not exist yet, and would report it in the wrong month. The
        press gauge is recorded (it drives yield and the nutrition volume) but it is
        NOT the booking volume. The booking gauge is taken after fermentation, when
        the lot is racked down.

      * A red presses WINE — fermentation is already over. That gauge IS the booking
        volume.

    So the default follows the path, and only an explicit override changes it.

    disposition GROSS_LEES → the juice settles and gets racked off the gross lees.
                             Opens a task, due settling_days out.
    disposition TO_BARREL  → straight on, no settling step.
    """
    if is_booking_volume is None:
        is_booking_volume = not presses_first(lot)
    pressed_at = pressed_at or timezone.now()
    total = _d(total_gal)
    if total is None or total <= 0:
        raise ValueError("Enter the gallons that came off the press.")

    if to_vessel is not None:
        ops.transfer_lot(lot, to_vessel, pressed_at)

    vm = VolumeMeasurement.objects.create(
        lot=lot, method=VolumeMeasurement.Method.PRESSURE_SENSOR,
        measured_at=_as_dt(pressed_at), volume_gal=total,
        is_booking_volume=is_booking_volume)

    ev = PressingEvent.objects.create(
        lot=lot, pressed_at=_as_dt(pressed_at),
        free_run_gal=_d(free_run_gal), press_gal=_d(press_gal),
        recombined=bool(recombined),
        settling_period_days=int(settling_days) if settling_days else None,
        disposition=disposition, volume=vm)

    if disposition == PressingEvent.Disposition.GROSS_LEES:
        lot.status = Lot.Status.SETTLING
        due = ops.add_working_days(_as_date(pressed_at), int(settling_days or 0))
        task_svc.create_task(
            title=f"Rack off gross lees — {lot.code}",
            body=f"{lot.code} pressed {_as_date(pressed_at)} — {total} gal settling for "
                 f"{settling_days} days. Rack off the gross lees, then inoculate.",
            due_date=due, lot=lot, actor=actor,
            dedupe_key=f"grosslees:{lot.pk}:{ev.pk}")
        if to_vessel is not None:
            from cellar.services import glycol as glycol_svc
            glycol_svc.on_cold_settling_rack(lot, to_vessel, actor=actor)
    else:
        lot.status = Lot.Status.PRESSED
    lot.save(update_fields=["status"])
    return ev


@transaction.atomic
def rack_off_gross_lees(lot, *, racked_at, clear_gal, to_vessel=None,
                        loss_reason="gross lees", actor=None):
    """Rack the settled juice off its gross lees. What didn't come off is a loss."""
    from cellar.models import VolumeLoss
    racked_at = racked_at or timezone.now()
    clear = _d(clear_gal)

    prior = VolumeMeasurement.booking_volume_for(lot)
    settled_from = _d(prior.volume_gal) if prior else None

    if to_vessel is not None:
        ops.transfer_lot(lot, to_vessel, racked_at)

    loss = None
    if settled_from is not None and clear is not None and settled_from > clear:
        loss = VolumeLoss.objects.create(
            lot=lot, volume_gal=(settled_from - clear).quantize(GAL),
            reason=loss_reason, occurred_at=_as_date(racked_at))

    # still juice — the production gauge comes after fermentation
    VolumeMeasurement.objects.create(
        lot=lot, method=VolumeMeasurement.Method.PRESSURE_SENSOR,
        measured_at=_as_dt(racked_at), volume_gal=clear, is_booking_volume=False)

    lot.status = Lot.Status.SETTLING
    lot.save(update_fields=["status"])

    # close the settling task
    from cellar.models import Task
    for t in Task.objects.filter(lot=lot, status=Task.Status.OPEN,
                                 dedupe_key__startswith=f"grosslees:{lot.pk}:"):
        task_svc.complete_task(t, actor=actor, detail=f"racked to {clear} gal clear juice")

    if to_vessel is not None:
        from cellar.services import glycol as glycol_svc
        glycol_svc.on_post_press_ferment_start(lot, to_vessel, actor=actor)

    return {"clear_gal": clear, "settled_from": settled_from, "loss": loss}


# ---------------------------------------------------------------- helpers
def _as_dt(d):
    from datetime import date as _date, datetime, time
    if isinstance(d, datetime):
        return d if timezone.is_aware(d) else timezone.make_aware(d)
    if isinstance(d, _date):
        return timezone.make_aware(datetime.combine(d, time(12, 0)))
    return d


def _as_date(d):
    from datetime import datetime
    return d.date() if isinstance(d, datetime) else d
