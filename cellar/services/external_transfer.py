"""
External transfer — a lot leaving the winery entirely: a bulk taxpaid sale, or
an in-bond move to another bonded premises. Distinct from a plain Movement
transfer (which only ever moves wine between the winery's own vessels).

TTB gate: unfermented juice/grapes were never produced as wine, so there is
nothing to report on the 5120.17 — Nate's rule is to detect that by the
absence of an InoculationEvent on the lot. Once a lot has been inoculated
(even if still fermenting), a sale of it is wine and needs the compliance
entry. `book_external_sale()` writes that entry itself (BulkTaxPaidRemoval or
BondTransfer OUT) in the same call, pre-filled with the gallons the caller
gauged — there's no separate "now go file it" step to forget.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (
    InoculationEvent, TankAssignment, BulkTaxPaidRemoval, BondTransfer,
)
from cellar.services.reporting import lot_tax_class

GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)).quantize(GAL) if v not in (None, "") else None


def _as_date(v):
    return timezone.localtime(v).date() if hasattr(v, "hour") else v


def is_wine(lot):
    """False for juice/grapes that have never been inoculated — nothing to
    report on the 5120.17 yet. True the moment fermentation has started, even
    mid-ferment (a sale at that stage is still a wine-account event)."""
    return InoculationEvent.objects.filter(lot=lot, voided_at__isnull=True).exists()


@transaction.atomic
def book_external_sale(lot, *, destination, gallons, at, kind, channel=None,
                       note="", actor=None):
    """Book `lot` leaving the winery to `destination` (an ExternalDestination).

    kind : 'taxpaid' -> writes a BulkTaxPaidRemoval (5120.17 line A14).
           'in_bond'  -> writes a BondTransfer OUT (to another bonded premises).
    Juice/grapes (no Inoculation event yet) skip the compliance entry
    entirely — `entry` comes back None and `wine` is False so the UI can say
    so plainly instead of implying something was filed.

    Always closes the lot's open tank assignment: the wine/juice is physically
    leaving the cellar, the same way rack-out or bottling does.
    """
    gal = _d(gallons)
    if gal is None or gal <= 0:
        raise ValueError("Enter the gallons leaving with this transfer.")
    at_dt = at or timezone.now()
    at_date = _as_date(at_dt)

    (TankAssignment.objects
     .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
     .update(emptied_at=at_dt))

    wine = is_wine(lot)
    entry = None
    if wine:
        counterparty = destination.name
        if getattr(destination, "bw_number", ""):
            counterparty = f"{destination.name} (BW-{destination.bw_number})"
        tax_class = lot_tax_class(lot)
        if kind == "in_bond":
            entry = BondTransfer.objects.create(
                lot=lot, direction=BondTransfer.Direction.OUT, tax_class=tax_class,
                gallons=gal, transferred_at=at_date,
                counterparty=counterparty, destination=destination)
        else:
            entry = BulkTaxPaidRemoval.objects.create(
                lot=lot, tax_class=tax_class, wine_gallons=gal, removed_at=at_date,
                channel=channel or BulkTaxPaidRemoval.Channel.WHOLESALE,
                destination=destination)
    return {"wine": wine, "entry": entry, "gallons": gal}
