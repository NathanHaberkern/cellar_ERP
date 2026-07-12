"""
Book-to-bond: the production booking for straight-fermentation wine.

`BookToBond` is what puts a non-fortified lot on line 2 of Part I — "produced by
fermentation" — and nothing has ever written one. The reporting layer reads the
table faithfully and the table is always empty, so a re-crushed 2025 would file a
5120.17 showing no production at all.

WHEN
    A lot books when its first authoritative volume exists:
      * whites / rosés — the press gauge (services/pressing.press)
      * reds           — the barrel-down after racking (VolumeMeasurement,
                         method=barrel_backfill: barrels × (capacity − headspace))
    `VolumeMeasurement.booking_volume_for()` already picks the highest-confidence
    gauge, so book_to_bond just reads it.

CONFIRM, DON'T INFER
    This is the number that goes on a federal form, so `draft()` prepares it and a
    human presses the button. Auto-writing a compliance figure off a cellar gauge
    that someone might still be revising is exactly the kind of silent wrong answer
    this system exists to prevent.

PORT DOES NOT BOOK HERE
    An INITIAL FortificationEvent *is* the production booking for a Port lot — it
    produces the base into col (a) line 2 and uses it out on line 19 in the same
    period. Booking a Port lot to bond as well would produce the wine twice.
    Both directions are guarded.
"""
from decimal import Decimal

from django.db import transaction

from cellar.models import BookToBond, FortificationEvent, TaxClass, VolumeMeasurement
from cellar.services import volumes as vol_svc

GAL = Decimal("0.1")


class AlreadyBooked(ValueError):
    pass


def is_booked(lot):
    return BookToBond.objects.filter(lot=lot, voided_at__isnull=True).exists()


def is_fortified(lot):
    return FortificationEvent.objects.filter(
        lot=lot, voided_at__isnull=True,
        kind=FortificationEvent.Kind.INITIAL).exists()


def _authoritative_gauge(lot):
    """A gauge EXPLICITLY flagged as the booking volume — nothing else will do.

    `VolumeMeasurement.booking_volume_for()` falls back to the best unflagged gauge
    when none is flagged, which is right for costing (any number beats no number) and
    wrong here. A white lot's press and lees gauges are juice, deliberately unflagged;
    falling back to them would let the system book 640 gallons of Verdelho JUICE as
    wine produced by fermentation. Production needs a gauge someone marked as the
    production gauge.
    """
    qs = [m for m in VolumeMeasurement.objects.filter(lot=lot, voided_at__isnull=True)
          if m.is_booking_volume]
    if not qs:
        return None
    return min(qs, key=lambda m: (VolumeMeasurement._RANK[m.confidence],
                                  -m.measured_at.timestamp(), -m.id))


def draft(lot, *, booked_at=None, gallons=None, tax_class=None):
    """What the booking WOULD say. Nothing is written."""
    vm = _authoritative_gauge(lot)
    gallons = (Decimal(str(gallons)).quantize(GAL) if gallons not in (None, "")
               else (Decimal(str(vm.volume_gal)).quantize(GAL) if vm and vm.volume_gal else None))

    problems = []
    if is_booked(lot):
        problems.append(f"{lot.code} is already booked to bond.")
    if is_fortified(lot):
        problems.append(
            f"{lot.code} has an initial fortification, which IS its production booking. "
            f"Booking it to bond as well would produce the wine twice.")
    if gallons is None:
        problems.append(
            f"{lot.code} has no gauged volume. Press it, or gauge the barrel-down, "
            f"before booking production.")

    return {
        "lot": lot,
        "gallons": gallons,
        "source": vm.get_method_display() if vm else None,
        "confidence": vm.confidence if vm else None,
        "measured_at": vm.measured_at if vm else None,
        "tax_class": tax_class or TaxClass.NOT_OVER_16,
        "booked_at": booked_at,
        "problems": problems,
        "ok": not problems,
    }


@transaction.atomic
def book_to_bond(lot, *, booked_at, gallons=None, tax_class=None, actor=None):
    """Book the lot's production. Blank gallons → the lot's booking-volume gauge."""
    d = draft(lot, booked_at=booked_at, gallons=gallons, tax_class=tax_class)
    if not d["ok"]:
        raise AlreadyBooked(" ".join(d["problems"]))

    return BookToBond.objects.create(
        lot=lot, booked_at=booked_at,
        gallons_produced=d["gallons"],
        tax_class=d["tax_class"])


def unbooked_lots():
    """Lots with a gauged volume and no production booking — the 'you forgot to book
    this' list. Drives a task rule and, eventually, a dashboard panel."""
    from cellar.models import Lot
    out = []
    for lot in Lot.objects.exclude(status=Lot.Status.BOTTLED).select_related(
            "current_designation"):
        if is_booked(lot) or is_fortified(lot):
            continue
        if _authoritative_gauge(lot) is None:
            continue
        out.append(lot)
    return out
