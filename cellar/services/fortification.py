"""
Fortification: the Port program's execution path.

The models already carry most of this — `FortificationEvent.save()` draws from the
HPGS account, derives spirit WG from PG at the blended proof, refuses to overdraw,
and prices the draw at the account's blended $/WG. What was missing was a service:
somewhere to compute how much spirit you actually need, to gauge the wine in and
out, to book the racking loss, and to keep an alcohol adjustment from being
reported as fermentation that never happened.

Two entry points:

    fortify(lot, ...)          Port fortified on skins. Base wine has just fermented,
                               is under 16%, has never been booked to bond. THIS EVENT
                               IS the production booking — do not also book_to_bond it.

    adjust_alcohol(lot, ...)   Spring racking. The wine is already in bond and already
                               in a tax class. Nothing is produced by fermentation. The
                               gap between (base + spirit) and the finished gauge is a
                               real loss and gets booked as one.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from cellar.models import (
    BookToBond, FortificationEvent, HighProofSpiritLedger, TaxClass, VolumeLoss,
)

CENT = Decimal("0.01")
GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)) if v is not None else None


class SpiritShortfall(ValueError):
    """The HPGS account can't cover the draw."""


# ======================================================================
# How much spirit do I need?
# ======================================================================
def pg_required(*, volume_gal, current_abv, target_abv, spirit_proof=None):
    """Proof gallons of high-proof spirit needed to lift `volume_gal` of wine from
    `current_abv` to `target_abv`.

    Pearson square, in alcohol units. The finished volume is the wine plus the
    spirit, so the spirit is diluting itself as well as fortifying the wine —
    solving for spirit WG (S) with wine volume V:

        V·c + S·s = (V + S)·t        →      S = V·(t − c) / (s − t)

    where s is the spirit's ABV (= proof / 2). Returns the figures the cellar needs
    plus the resulting finished volume, so the gauge can be checked against it.
    """
    V = _d(volume_gal)
    c = _d(current_abv)
    t = _d(target_abv)
    proof = _d(spirit_proof) if spirit_proof else _d(HighProofSpiritLedger.current_blended_proof())
    if not proof:
        raise SpiritShortfall("The HPGS account is empty — record a spirit receipt first.")
    s = proof / 2

    if t <= c:
        raise ValueError(f"Target {t}% is not above the current {c}%.")
    if s <= t:
        raise ValueError(f"Spirit at {proof} proof ({s}% ABV) can't lift wine to {t}%.")

    spirit_wg = (V * (t - c) / (s - t)).quantize(CENT, ROUND_HALF_UP)
    spirit_pg = (spirit_wg * proof / 100).quantize(CENT, ROUND_HALF_UP)
    finished = (V + spirit_wg).quantize(GAL, ROUND_HALF_UP)

    on_hand = _d(HighProofSpiritLedger.on_hand_wg())
    return {
        "spirit_wg": spirit_wg,
        "proof_gallons": spirit_pg,
        "spirit_proof": proof.quantize(CENT),
        "spirit_abv": s.quantize(CENT),
        "finished_wg_expected": finished,
        "hpgs_on_hand_wg": on_hand,
        "sufficient": on_hand >= spirit_wg,
    }


def tax_class_for_abv(abv):
    abv = _d(abv)
    if abv <= 16:
        return TaxClass.NOT_OVER_16
    if abv <= 21:
        return TaxClass.OVER_16_21
    return TaxClass.OVER_21_24


# ======================================================================
# Initial fortification — Port on skins
# ======================================================================
@transaction.atomic
def fortify(lot, *, fortified_on_skins_date, booked_at, proof_gallons_drawn,
            finished_wg=None, target_abv=None, expected_tax_class=None,
            spirit_proof=None, actor=None):
    """Port fortified on skins. Books the base into col (a) and the finished wine
    into its class; `FortificationEvent.save()` draws the spirit from HPGS.

    finished_wg (T) blank → the lot's booking-volume measurement.
    """
    if expected_tax_class is None:
        if target_abv is None:
            raise ValueError("Give either expected_tax_class or target_abv.")
        expected_tax_class = tax_class_for_abv(target_abv)

    if BookToBond.objects.filter(lot=lot, voided_at__isnull=True).exists():
        raise ValueError(
            f"{lot.code} already has a book-to-bond. A fortification IS the production "
            f"booking for a Port lot — booking both would produce the wine twice.")

    fe = FortificationEvent(
        lot=lot,
        kind=FortificationEvent.Kind.INITIAL,
        base_tax_class=TaxClass.NOT_OVER_16,     # fresh base wine is under 16%
        fortified_on_skins_date=fortified_on_skins_date,
        booked_at=booked_at,
        proof_gallons_drawn=_d(proof_gallons_drawn),
        finished_wg=_d(finished_wg),
        spirit_proof=_d(spirit_proof),
        expected_tax_class=expected_tax_class,
    )
    fe.save()
    return fe


# ======================================================================
# Alcohol adjustment — spring racking
# ======================================================================
@transaction.atomic
def adjust_alcohol(lot, *, adjusted_at, proof_gallons_drawn, base_wg, finished_wg,
                   base_tax_class=None, expected_tax_class=None, spirit_proof=None,
                   loss_reason="racking loss (alcohol adjustment)", actor=None):
    """Re-fortify wine already in bond — the spring racking top-up.

    base_wg     : the gauge going IN  (the wine you racked)
    finished_wg : the gauge coming OUT (after the spirit is in)

    Both are required. The difference between (base + spirit) and finished is wine
    that did not come out of the barrel, and it is booked as a loss on line 29
    rather than silently disappearing into the production figure — which is exactly
    what happened in June 2025 (11.3 gal).
    """
    from cellar.services.reporting import lot_tax_class

    if base_tax_class is None:
        base_tax_class = lot_tax_class(lot)
    if expected_tax_class is None:
        expected_tax_class = base_tax_class     # a top-up rarely changes class

    fe = FortificationEvent(
        lot=lot,
        kind=FortificationEvent.Kind.ADJUSTMENT,
        base_tax_class=base_tax_class,
        fortified_on_skins_date=adjusted_at,     # no skins involved; same date
        booked_at=adjusted_at,
        proof_gallons_drawn=_d(proof_gallons_drawn),
        base_wg=_d(base_wg),                     # supplied, NOT derived
        finished_wg=_d(finished_wg),
        spirit_proof=_d(spirit_proof),
        expected_tax_class=expected_tax_class,
    )
    fe.save()

    loss = fe.implied_loss
    if loss and loss > 0:
        VolumeLoss.objects.create(
            lot=lot, volume_gal=loss, reason=loss_reason, occurred_at=adjusted_at)
    elif loss and loss < 0:
        raise ValueError(
            f"The finished gauge ({finished_wg}) exceeds base + spirit "
            f"({fe.base_wg} + {fe.spirit_wg}) by {-loss} gal. Wine cannot appear "
            f"from nowhere — re-check the gauges.")
    return fe
