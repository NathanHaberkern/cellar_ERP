"""
Sweetening: adding concentrate to reach a target residual sugar (measured in Brix).

Sweetening happens in bulk (tank) or per-lot, pre-bottling. The concentrate (vino blanc)
is additive: 100 gal wine + 5 gal concentrate = 105 gal finished wine. `SweeteningEvent`
auto-creates a `MaterialTransaction` (Part IV ledger) via its save() method.

Target residual sugar is typically 0.25–0.35% by weight, measured post-sweeten as Brix.
"""
from decimal import Decimal

from django.db import transaction

from cellar.models import Material, SweeteningEvent, MaterialTransaction, VolumeMeasurement, TaxClass

CENT = Decimal("0.01")
GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)).quantize(CENT) if v not in (None, "") else None


class ConcentrateNotFound(ValueError):
    """Vino blanc not in the Material catalog."""


def vino_blanc_material():
    """The vino blanc concentrate from the Material catalog, or raise if missing."""
    mat = Material.objects.filter(name__icontains="vino blanc").first()
    if mat is None:
        raise ConcentrateNotFound(
            "Vino blanc not found in Material catalog. Seed it first "
            "(name='Vino Blanc concentrate', kind='concentrate', unit='gal', unit_cost=...).")
    return mat


@transaction.atomic
def sweeten(lot, *, sweetened_at, concentrate_gal, brix_before=None, brix_after=None,
            tax_class=None, actor=None):
    """Sweeten a lot with vino blanc concentrate.

    concentrate_gal : gallons of vino blanc added (additive: final = wine + concentrate)
    brix_before     : Brix measurement before sweetening (optional)
    brix_after      : Brix measurement after sweetening (optional)
    tax_class       : tax class of the sweetened wine (defaults to NOT_OVER_16)

    SweeteningEvent auto-creates a MaterialTransaction for the concentrate used.
    Returns the SweeteningEvent.
    """
    from cellar.services import volumes as vol_svc

    conc_gal = _d(concentrate_gal)
    if conc_gal is None or conc_gal <= 0:
        raise ValueError("Enter the gallons of concentrate added.")

    # Current lot volume (wine that will be sweetened)
    pre_vol = vol_svc.lot_balance(lot)
    if pre_vol is None or pre_vol <= 0:
        raise ValueError(f"{lot.code} has no wine to sweeten.")

    # Finished volume = wine + concentrate (additive)
    finished = (pre_vol + conc_gal).quantize(GAL)

    # Material
    mat = vino_blanc_material()

    # Create the event (save() auto-creates MaterialTransaction)
    event = SweeteningEvent.objects.create(
        lot=lot, sweetened_at=sweetened_at,
        tax_class=tax_class or TaxClass.NOT_OVER_16,
        volume_used=_d(pre_vol),
        concentrate=mat,
        concentrate_gallons=conc_gal,
        brix_before=_d(brix_before),
        brix_after=_d(brix_after))

    # Record the gauge after sweetening (for costing/reference)
    vm = VolumeMeasurement.objects.create(
        lot=lot, method=VolumeMeasurement.Method.STATED,
        measured_at=sweetened_at, volume_gal=finished,
        is_booking_volume=False)

    return event
