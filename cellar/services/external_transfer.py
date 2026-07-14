"""
External transfer — a lot leaving the winery entirely: a bulk taxpaid sale, an
in-bond move to another bonded premises, or a bulk sale of must/juice that has
never been inoculated. Distinct from a plain Movement transfer (which only
ever moves wine between the winery's own vessels).

TTB gate: unfermented juice/grapes were never produced as wine, so there is
nothing to report on the 5120.17 — Nate's rule is to detect that by the
absence of an InoculationEvent on the lot. Once a lot has been inoculated
(even if still fermenting), a sale of it is wine and needs the compliance
entry. `book_external_sale()` writes that entry itself (BulkTaxPaidRemoval,
BondTransfer OUT, or MustSale) in the same call, pre-filled with the gallons
the caller gauged — there's no separate "now go file it" step to forget.

kind is validated against fermentation state rather than left to the caller:
'taxpaid'/'in_bond' are wine-only (they carry a TTB tax class that unfermented
juice doesn't have), 'must_sale' is juice/must-only. Picking the wrong one for
the lot's actual state raises rather than silently booking something that
doesn't make TTB sense.

PARTIAL vs FULL: earlier versions always closed the lot's open tank assignment
on any external transfer, which was only correct for a sale of the whole lot.
Selling 100 gal out of a 965 gal tank does not empty the tank — the assignment
now only closes when the amount leaving covers (within a tenth of a gallon)
what `volumes.lot_balance()` says is actually in the lot. A true partial sale
just books the removal and leaves the lot right where it is.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (
    InoculationEvent, TankAssignment, BulkTaxPaidRemoval, BondTransfer, MustSale,
)
from cellar.services.reporting import lot_tax_class

GAL = Decimal("0.1")

KIND_TAXPAID = "taxpaid"
KIND_IN_BOND = "in_bond"
KIND_MUST_SALE = "must_sale"
KINDS = (KIND_TAXPAID, KIND_IN_BOND, KIND_MUST_SALE)


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v not in (None, "") else None


def _as_date(v):
    return timezone.localtime(v).date() if hasattr(v, "hour") else v


def is_wine(lot):
    """False for juice/grapes that have never been inoculated — nothing to
    report on the 5120.17 yet, and nothing with a TTB tax class to sell as
    'wine'. True the moment fermentation has started, even mid-ferment (a sale
    at that stage is still a wine-account event)."""
    return InoculationEvent.objects.filter(lot=lot, voided_at__isnull=True).exists()


@transaction.atomic
def book_external_sale(lot, *, destination, gallons, at, kind, channel=None,
                       note="", actor=None):
    """Book `lot` leaving the winery to `destination` (an ExternalDestination).

    kind : 'taxpaid'   -> writes a BulkTaxPaidRemoval (5120.17 line A14). Wine only.
           'in_bond'    -> writes a BondTransfer OUT (to another bonded premises). Wine only.
           'must_sale'  -> writes a MustSale. Unfermented juice/must only — not a
                           TTB wine-gallon event, since it was never produced as wine.

    Always closes the lot's open tank assignment ONLY if this transfer covers
    the lot's full current balance (see volumes.lot_balance()) — a partial sale
    leaves the lot in its vessel with a reduced balance instead.
    """
    from cellar.services import volumes as vol_svc

    if kind not in KINDS:
        raise ValueError(f"Unknown transfer kind '{kind}'.")

    gal = _d(gallons)
    if gal is None or gal <= 0:
        raise ValueError("Enter the gallons leaving with this transfer.")
    at_dt = at or timezone.now()
    at_date = _as_date(at_dt)

    wine = is_wine(lot)
    if kind in (KIND_TAXPAID, KIND_IN_BOND) and not wine:
        raise ValueError(
            f"{lot.code} hasn't been inoculated yet — it's still juice/must, "
            "not wine, so there's no tax class to sell it under. Use 'Bulk sale "
            "of must/juice' instead.")
    if kind == KIND_MUST_SALE and wine:
        raise ValueError(
            f"{lot.code} has already been inoculated — it's wine now, not must. "
            "Use a bulk taxpaid sale or in-bond transfer instead.")

    balance = vol_svc.lot_balance(lot)
    full_sale = balance is None or gal >= (balance - GAL)

    if full_sale:
        (TankAssignment.objects
         .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
         .update(emptied_at=at_dt))

    entry = None
    if kind == KIND_MUST_SALE:
        entry = MustSale.objects.create(
            lot=lot, gallons=gal, sold_at=at_date, destination=destination, notes=note)
    else:
        counterparty = destination.name
        if getattr(destination, "bw_number", ""):
            counterparty = f"{destination.name} (BW-{destination.bw_number})"
        tax_class = lot_tax_class(lot)
        if kind == KIND_IN_BOND:
            entry = BondTransfer.objects.create(
                lot=lot, direction=BondTransfer.Direction.OUT, tax_class=tax_class,
                gallons=gal, transferred_at=at_date,
                counterparty=counterparty, destination=destination)
        else:
            entry = BulkTaxPaidRemoval.objects.create(
                lot=lot, tax_class=tax_class, wine_gallons=gal, removed_at=at_date,
                channel=channel or BulkTaxPaidRemoval.Channel.WHOLESALE,
                destination=destination)
    return {"wine": wine, "entry": entry, "gallons": gal, "full_sale": full_sale}
