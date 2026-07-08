"""
TTB F 5000.24 wine excise — CBMA engine.

Wine tax rates (26 U.S.C. 5041(b), per wine gallon):
    a  ≤16% ......... $1.07
    b  16–21% ....... $1.57
    c  21–24% ....... $3.15
CBMA credit (per producer per calendar year, one pool across ALL classes, applied to
gallons in order of removal, capped at 750,000 gal):
    first  30,000 gal ....... $1.00 / gal   (hard cider: $0.062)
    next  100,000 gal ....... $0.90 / gal
    next  620,000 gal ....... $0.535 / gal
    beyond 750,000 gal ...... no credit

The credit tiers are the "annual maximum" — the engine walks removals chronologically,
consumes the pool, and never credits a gallon beyond its tier, so you can't over-claim.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from cellar.models import TaxPaidRemoval
from cellar.services.reporting import lot_tax_class

WINE_TAX_RATES = {"a": Decimal("1.07"), "b": Decimal("1.57"), "c": Decimal("3.15")}
CBMA_TIERS = [(Decimal("30000"), Decimal("1.00")),      # cumulative ceiling, credit/gal
              (Decimal("130000"), Decimal("0.90")),
              (Decimal("750000"), Decimal("0.535"))]
CENT = Decimal("0.01")


def credit_for_block(start_cum, gallons):
    """Credit for `gallons` removed starting at year-to-date cumulative `start_cum`,
    split across tier boundaries. Gallons beyond 750,000 earn nothing."""
    credit = Decimal("0")
    cum, remaining = Decimal(start_cum), Decimal(gallons)
    for ceiling, rate in CBMA_TIERS:
        if cum >= ceiling or remaining <= 0:
            continue
        take = min(remaining, ceiling - cum)
        credit += take * rate
        cum += take
        remaining -= take
    return credit


def excise_on_removals(removals, period_start, period_end):
    """removals: iterable of (removed_at, wine_gallons, tax_class), any order.
    Computes tax for the period, using calendar-YTD cumulative for the credit tier."""
    year = period_start.year
    rows = sorted((r for r in removals if r[0] >= date(year, 1, 1) and r[0] < period_end),
                  key=lambda r: r[0])
    cum = Decimal("0")
    gross = defaultdict(Decimal)
    gallons = defaultdict(Decimal)
    credit = Decimal("0")
    for when, g, cls in rows:
        g = Decimal(str(g))
        if period_start <= when < period_end:
            gross[cls] += g * WINE_TAX_RATES.get(cls, WINE_TAX_RATES["a"])
            credit += credit_for_block(cum, g)
            gallons[cls] += g
        cum += g
    gross_total = sum(gross.values())
    net = gross_total - credit
    return {
        "gallons_by_class": {c: g for c, g in gallons.items()},
        "gross_tax": gross_total.quantize(CENT, ROUND_HALF_UP),
        "cbma_credit": credit.quantize(CENT, ROUND_HALF_UP),
        "net_tax": net.quantize(CENT, ROUND_HALF_UP),
        "ytd_gallons_through_period": cum,
    }


def compute_period_excise(year, period_start, period_end):
    """Reads bottled + bulk taxpaid removals from the ledger and computes the 5000.24 wine line."""
    from cellar.models import BulkTaxPaidRemoval
    yr_start = date(year, 1, 1)
    bottled = TaxPaidRemoval.objects.filter(
        voided_at__isnull=True, removed_at__gte=yr_start, removed_at__lt=period_end
    ).select_related("bottling_run")
    bulk = BulkTaxPaidRemoval.objects.filter(
        voided_at__isnull=True, removed_at__gte=yr_start, removed_at__lt=period_end)
    removals = [(r.removed_at, r.wine_gallons_removed, lot_tax_class(r.bottling_run.source_lot))
                for r in bottled]
    removals += [(r.removed_at, r.wine_gallons, r.tax_class) for r in bulk]
    return excise_on_removals(removals, period_start, period_end)
