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


# Vino blanc runs around 68 Brix. Overridable per call — bench-check the pail.
DEFAULT_CONCENTRATE_BRIX = Decimal("68")


def concentrate_gallons_for_rs(wine_gal, target_rs_pct,
                               concentrate_brix=DEFAULT_CONCENTRATE_BRIX):
    """Gallons of concentrate needed to bring `wine_gal` up to `target_rs_pct` RS.

    The cellar thinks in residual sugar ("add .25% RS Vino Blanc"), not in gallons
    of concentrate, so this converts. Sugar mass balance against the FINAL volume:

        conc_gal * conc_brix = (wine_gal + conc_gal) * target
    =>  conc_gal = wine_gal * target / (conc_brix - target)

    The denominator carries the concentrate's strength MINUS the target because the
    concentrate is itself adding volume that also has to be brought to the target —
    dividing by conc_brix alone would under-dose.
    """
    wine = _d(wine_gal)
    target = _d(target_rs_pct)
    brix = _d(concentrate_brix)
    if wine is None or target is None or brix is None:
        raise ValueError("Need the wine volume, the target RS %, and the concentrate's Brix.")
    if target <= 0:
        raise ValueError("Target RS must be greater than zero.")
    if brix <= target:
        raise ValueError(
            f"The concentrate is {brix:g} Brix — it can't sweeten wine to {target:g}% RS.")
    return (wine * target / (brix - target)).quantize(GAL)


@transaction.atomic
def sweeten(lot, *, sweetened_at, concentrate_gal=None, target_rs_pct=None,
            concentrate_brix=DEFAULT_CONCENTRATE_BRIX,
            brix_before=None, brix_after=None, tax_class=None, actor=None):
    """Sweeten a lot with vino blanc concentrate.

    Give EITHER concentrate_gal, OR target_rs_pct (the gallons are then derived
    via concentrate_gallons_for_rs — this is how ".25% RS" gets entered).

    concentrate_gal : gallons of vino blanc added (additive: final = wine + concentrate)
    brix_before     : Brix measurement before sweetening (optional)
    brix_after      : Brix measurement after sweetening (optional)
    tax_class       : tax class of the sweetened wine (defaults to NOT_OVER_16)

    SweeteningEvent auto-creates a MaterialTransaction for the concentrate used.
    Returns the SweeteningEvent.
    """
    from cellar.services import volumes as vol_svc

    # Current lot volume (wine that will be sweetened) — needed up front now, since
    # an RS target is computed against it.
    pre_vol = vol_svc.lot_balance(lot)
    if pre_vol is None or pre_vol <= 0:
        raise ValueError(f"{lot.code} has no wine to sweeten.")

    conc_gal = _d(concentrate_gal)
    if conc_gal is None:
        if target_rs_pct in (None, ""):
            raise ValueError(
                "Enter either the gallons of concentrate, or a target RS % to "
                "compute them from.")
        conc_gal = concentrate_gallons_for_rs(pre_vol, target_rs_pct, concentrate_brix)
    if conc_gal is None or conc_gal <= 0:
        raise ValueError("Enter the gallons of concentrate added.")

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

    # NOTE: no VolumeMeasurement is written here, deliberately. This function used
    # to record a "finished" gauge, which looked like it grossed the lot up but
    # didn't: lot_balance() reads the single highest-confidence/flagged gauge
    # rather than summing gauges, so a fresh STATED row was simply ignored (and
    # had it won the confidence race it would have become `booked`, causing every
    # prior removal to be netted out twice). The concentrate is instead counted
    # once, from this event, in volumes.volume_added_gal() — so the lot now really
    # does read used + concentrate afterwards.
    return event


def sweetenings_of(lot):
    return (SweeteningEvent.objects.filter(lot=lot, voided_at__isnull=True)
            .select_related("concentrate").order_by("-sweetened_at", "-id"))
