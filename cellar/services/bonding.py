"""
Book-to-bond — the production declaration, and the lifecycle gate.

WHY THIS MODULE EXISTS
----------------------
`DONE_PRIMARY` used to have exactly one entry point: `fermentation.rack_to_barrel()`.
That welded the end of primary to a *physical* act (putting wine in oak) when the
thing that actually ends primary is a *declaration* (booking the produced gallons to
bond). Any wine that never sees oak — Verdelho, which is racked, booked, and bottled
out of tank — was therefore structurally stranded at PRESSED/SETTLING: no Bottling
tab, no Additions tab, Fermentation stuck on screen forever.

Meanwhile `BookToBond` — the single most compliance-critical event in the system,
read by `reporting.py`, by `aging._lot_volume()`, and by the disposition badge — had
no UI at all. It was reachable only through Django admin.

So: booking to bond is now its own action, and it is what flips the lot to
DONE_PRIMARY. Racking to barrel is demoted to what it actually is — an aging move
(see `barreling.py`) — and it no longer touches status.

ORDERING IS DELIBERATELY FREE
-----------------------------
Booking and barreling are decoupled. Neither gates the other, because the gauge can
come from either direction:

  * TANK GAUGE   — pressure-sensor tanks. The VolumeMeasurement is the number.
                   Book off the tank, then barrel down later (or never).
  * BARREL FILL  — tanks with no sensor. The barrel-down IS the gauge: you fill the
                   barrels, sum the actual fills, and *that* is the booking volume.
                   Barrel down first, then book.

A partial barrel-down (some barrels now, remainder held in tank until more barrels
free up) leaves the tank assignment open and books nothing; the barrel-fill gauge
simply sums every open placement, so it converges on the true figure as the lot goes
down over several sessions. Book to bond once the lot is fully down.

TAX CLASS
---------
Default is col a (≤16%) for everything, including Port BASE wine — the base wine is
under 16% when it is booked, and the move to col b happens at fortification via
`FortificationEvent.expected_tax_class`. Booking Port base to col b is a known error
in previously-filed reports and is NOT reproduced here. The field stays editable, but
the default is the correct one.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (AgingPlacement, BookToBond, Lot, TaxClass,
                           VolumeMeasurement)

# Booking is available from the moment the wine is off its skins and gauged.
BOOKABLE = {Lot.Status.PRESSED, Lot.Status.SETTLING, Lot.Status.FERMENTING}


class GaugeSource:
    """Mirrors BookToBond.GaugeSource — how the booking volume was arrived at."""
    TANK = "tank_gauge"
    BARREL = "barrel_fill"
    STATED = "stated"


# ------------------------------------------------------------------ predicates
def is_in_bond(lot) -> bool:
    """A lot is in bond once production is declared — a BookToBond (straight
    ferment) or a FortificationEvent (Port, which books via the fortification and
    never gets a BookToBond row of its own). Check BOTH, or every Port lot reads
    'In fermenter' forever."""
    return (lot.bond_bookings.filter(voided_at__isnull=True).exists()
            or lot.fortifications.filter(voided_at__isnull=True).exists())


def can_book_to_bond(lot) -> bool:
    """Off the skins, not already booked, and not a bottling parcel (a parcel
    inherits its parent's bond status — it is not a separate production)."""
    from cellar.services import bottling as bz
    return (lot.status in BOOKABLE
            and not is_in_bond(lot)
            and not bz.is_parcel(lot))


# ---------------------------------------------------------------------- gauges
def barrel_fill_total(lot):
    """Σ of every open barrel/aging placement on the lot — the barrel-fill gauge.

    Sums ACTUAL recorded fills, not capacity, and sums across multiple barrel-down
    sessions, so a lot that went down 6 barrels in October and 4 more in November
    gauges correctly at the total of all ten.
    """
    rows = lot.placements.filter(emptied_at__isnull=True, voided_at__isnull=True)
    total = sum((p.volume_gal or Decimal("0") for p in rows), Decimal("0"))
    return total.quantize(Decimal("0.1")) if total else None


def tank_gauge(lot):
    """The most recent measured tank volume, if there is one."""
    vm = (VolumeMeasurement.objects
          .filter(lot=lot, voided_at__isnull=True)
          .exclude(volume_gal__isnull=True)
          .order_by("-measured_at", "-id").first())
    if not vm:
        return None
    return {"volume_gal": vm.volume_gal, "method": vm.get_method_display(),
            "measured_at": vm.measured_at, "pk": vm.pk}


def gauge_options(lot):
    """What the cellar can book off, with the recommended default preselected.

    Barrel fill wins when the lot is fully down (no open tank assignment) — that's
    the no-sensor path and the placements ARE the gauge. Otherwise the tank gauge
    wins if one exists. Stated is always available as the manual fallback.
    """
    from cellar.models import TankAssignment
    in_tank = TankAssignment.objects.filter(
        lot=lot, voided_at__isnull=True, emptied_at__isnull=True).exists()

    tank = tank_gauge(lot)
    barrel = barrel_fill_total(lot)
    n_barrels = lot.placements.filter(emptied_at__isnull=True,
                                      voided_at__isnull=True).count()

    if barrel and not in_tank:
        default = GaugeSource.BARREL
    elif tank:
        default = GaugeSource.TANK
    else:
        default = GaugeSource.STATED

    return {
        "tank": tank,
        "barrel_total": barrel,
        "barrel_count": n_barrels,
        "still_in_tank": in_tank,
        "default": default,
        "default_volume": (barrel if default == GaugeSource.BARREL
                           else (tank["volume_gal"] if tank else None)),
    }


def default_tax_class(lot):
    """Col a for everything at booking. See the module docstring — Port base wine
    is under 16% at the time it is booked; fortification moves it to col b."""
    return TaxClass.NOT_OVER_16


# ----------------------------------------------------------------------- action
@transaction.atomic
def book_to_bond(lot, *, gallons_produced, gauge_source=GaugeSource.STATED,
                 booked_at=None, tax_class=None, actor=None):
    """Declare production. Flips the lot to DONE_PRIMARY.

    Writes the authoritative gauge as an `is_booking_volume` VolumeMeasurement (so
    `booking_volume_for()` and every downstream cost / composition / 5120.17 read
    resolves to exactly this number), then the BookToBond row that points at it.
    """
    if is_in_bond(lot):
        raise ValueError(f"{lot.code} is already booked to bond.")
    if lot.status not in BOOKABLE:
        raise ValueError(
            f"{lot.code} is {lot.get_status_display()} — book to bond once it is "
            f"off the skins (pressed or settling).")

    gallons = Decimal(str(gallons_produced or "0"))
    if gallons <= 0:
        raise ValueError("Enter the produced gallons — this is the figure that "
                         "goes on the 5120.17.")

    booked_at = booked_at or timezone.localdate()
    tax_class = tax_class or default_tax_class(lot)

    method = {
        GaugeSource.TANK: VolumeMeasurement.Method.PRESSURE_SENSOR,
        GaugeSource.BARREL: VolumeMeasurement.Method.BARREL_BACKFILL,
    }.get(gauge_source, VolumeMeasurement.Method.STATED)

    vm = VolumeMeasurement(
        lot=lot, method=method,
        measured_at=timezone.now(),
        volume_gal=gallons.quantize(Decimal("0.1")),
        is_booking_volume=True)
    if method == VolumeMeasurement.Method.BARREL_BACKFILL:
        # volume_gal is already the sum of ACTUAL fills — record the barrel count
        # for the audit trail but do NOT let the model's capacity-minus-headspace
        # fallback recompute it (it only fires when volume_gal is blank).
        vm.barrels_filled = lot.placements.filter(
            emptied_at__isnull=True, voided_at__isnull=True).count()
    vm.save()

    booking = BookToBond.objects.create(
        lot=lot, booked_at=booked_at,
        gallons_produced=gallons.quantize(Decimal("0.1")),
        tax_class=tax_class,
        gauge_source=gauge_source,
        volume=vm)

    lot.status = Lot.Status.DONE_PRIMARY
    lot.save(update_fields=["status"])
    return booking


def booking_for(lot):
    return (lot.bond_bookings.filter(voided_at__isnull=True)
            .order_by("-booked_at").first())
