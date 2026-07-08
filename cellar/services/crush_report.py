"""
California Grape Crush Report — aggregation.

Groups the crush year's weigh tags by pricing district × variety, reporting tons
crushed, and for PURCHASED fruit the purchased tons and weighted-average price ($/ton)
and Brix. Estate fruit is counted in tonnage but carries no price (as on the report).

St. Amant sources from District 10 (Amador) and District 11 (Lodi).
The crush district lives on the Vineyard; tonnage comes from the weigh tag.
"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from cellar.models import WeighTag

CENT = Decimal("0.01")
TENTH = Decimal("0.1")


def ca_crush_report(year):
    """Returns rows keyed (district, variety) with tons, purchased tons, avg price, avg Brix."""
    acc = defaultdict(lambda: {"tons": Decimal("0"), "purchased_tons": Decimal("0"),
                               "price_wsum": Decimal("0"), "brix_wsum": Decimal("0"),
                               "brix_w": Decimal("0")})
    tags = (WeighTag.objects
            .filter(harvest_event__harvest_date__year=year)
            .select_related("harvest_event__block__variety",
                            "harvest_event__block__vineyard"))
    for wt in tags:
        block = wt.harvest_event.block
        variety = block.variety.name
        district = block.vineyard.crush_district
        tons = Decimal(str(wt.net_tons))
        row = acc[(district, variety)]
        row["tons"] += tons
        if wt.source_type == "purchased" and wt.purchase_price_per_ton:
            row["purchased_tons"] += tons
            row["price_wsum"] += tons * wt.purchase_price_per_ton
        if wt.brix_at_receipt:
            row["brix_wsum"] += tons * wt.brix_at_receipt
            row["brix_w"] += tons

    out = []
    for (district, variety), r in sorted(acc.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1])):
        avg_price = (r["price_wsum"] / r["purchased_tons"]).quantize(CENT, ROUND_HALF_UP) \
            if r["purchased_tons"] else None
        avg_brix = (r["brix_wsum"] / r["brix_w"]).quantize(TENTH, ROUND_HALF_UP) \
            if r["brix_w"] else None
        out.append({
            "district": district, "variety": variety,
            "tons": r["tons"].quantize(Decimal("0.001"), ROUND_HALF_UP),
            "purchased_tons": r["purchased_tons"].quantize(Decimal("0.001"), ROUND_HALF_UP),
            "avg_price_per_ton": avg_price, "avg_brix": avg_brix,
        })
    return out


def crush_report_totals(rows):
    """District-level and grand totals for the crush report."""
    by_district = defaultdict(lambda: Decimal("0"))
    grand = Decimal("0")
    for r in rows:
        by_district[r["district"]] += r["tons"]
        grand += r["tons"]
    return {"by_district": dict(by_district), "grand_total_tons": grand}
