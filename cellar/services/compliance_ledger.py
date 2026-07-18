"""
Per-lot in-bond compliance ledger — the dated, itemised view behind the
Compliance tile (and, later, the lot-level backing for the 5120.17 operations
report).

`volumes.lot_balance_detail` gives the signed *totals*; this expands them into
the individual dated events that produced each total, in chronological order,
with a running balance. It is built entirely from the same source models and
the same component functions volumes.py aggregates, so the sum of the rows
reconciles exactly to `volumes.lot_balance(lot)` by construction:

    booked            (+)  the production declaration (book-to-bond / fortify T)
    inbound lineage   (+)  wine given to this lot (blend in, topping in)
    volume added      (+)  water + sweetening concentrate
    outbound lineage  (−)  wine this lot gave away
    losses            (−)  evaporation / spillage
    bottled           (−)  bottling runs (incl. bottling loss)
    bulk removed      (−)  bulk tax-paid removals
    bond transfer out (−)  in-bond B2B transfers out
    must sold         (−)  bulk juice/must sales

Read-only. Every row is tied to a real recorded event; nothing here writes.
"""
from decimal import Decimal

from cellar.models import (
    Addition, Additive, BondTransfer, BookToBond, BottlingRun, BulkTaxPaidRemoval,
    FortificationEvent, LotLineage, MustSale, SweeteningEvent, VolumeLoss,
)
from cellar.services import volumes as vol

ZERO = Decimal("0")
GAL = Decimal("0.1")


def _d(v):
    return Decimal(str(v)) if v is not None else ZERO


def _dt(x):
    """Normalise a date or datetime to a date for chronological sorting."""
    return getattr(x, "date", lambda: x)() if hasattr(x, "hour") else x


def _booking_date(lot):
    dates = []
    b = (BookToBond.objects.filter(lot=lot, voided_at__isnull=True)
         .order_by("booked_at").values_list("booked_at", flat=True).first())
    if b:
        dates.append(b)
    f = (FortificationEvent.objects.filter(lot=lot, voided_at__isnull=True)
         .exclude(booked_at__isnull=True)
         .order_by("booked_at").values_list("booked_at", flat=True).first())
    if f:
        dates.append(f)
    return min(dates) if dates else None


def rows(lot):
    """Chronological ledger rows with a running balance.

    Returns {"rows": [...], "balance": Decimal|None, "reconciles": bool}.
    Each row: {date, label, detail, increase, decrease}.
    """
    items = []

    # + booked production figure (one row = the net booked_volume volumes uses)
    booked = vol.booked_volume(lot)
    if booked is not None and booked != ZERO:
        items.append({
            "date": _booking_date(lot), "label": "Booked to bond",
            "detail": "production gauge", "increase": _d(booked).quantize(GAL),
            "decrease": None,
        })

    # + inbound liquid lineage (blend in / topping in)
    for e in (LotLineage.objects.filter(
            child_lot=lot, voided_at__isnull=True,
            relationship_type__in=vol._LIQUID_EDGES).select_related("parent_lot")):
        items.append({
            "date": _dt(e.created_at), "label": e.get_relationship_type_display(),
            "detail": f"from {getattr(e.parent_lot, 'code', '—')}",
            "increase": _d(e.volume_gal).quantize(GAL), "decrease": None,
        })

    # + volume added: water (pct-volume additives) + sweetening concentrate
    for a in (Addition.objects.filter(
            lot=lot, voided_at__isnull=True,
            additive__dose_mode=Additive.DoseMode.PCT_VOLUME).select_related("additive")):
        items.append({
            "date": _dt(a.added_at), "label": "Water addition",
            "detail": a.additive.name, "increase": _d(a.quantity).quantize(GAL),
            "decrease": None,
        })
    for s in SweeteningEvent.objects.filter(lot=lot, voided_at__isnull=True):
        items.append({
            "date": _dt(s.sweetened_at), "label": "Backsweeten",
            "detail": "concentrate", "increase": _d(s.concentrate_gallons).quantize(GAL),
            "decrease": None,
        })

    # − outbound liquid lineage (blend out / topping out / saignée / drain-off)
    for e in (LotLineage.objects.filter(
            parent_lot=lot, voided_at__isnull=True,
            relationship_type__in=vol._LIQUID_EDGES).select_related("child_lot")):
        items.append({
            "date": _dt(e.created_at), "label": e.get_relationship_type_display(),
            "detail": f"to {getattr(e.child_lot, 'code', '—')}",
            "increase": None, "decrease": _d(e.volume_gal).quantize(GAL),
        })

    # − losses
    for l in VolumeLoss.objects.filter(lot=lot, voided_at__isnull=True):
        items.append({
            "date": _dt(l.occurred_at), "label": "Volume loss",
            "detail": l.reason or "", "increase": None,
            "decrease": _d(l.volume_gal).quantize(GAL),
        })

    # − bottled (wine bottled + bottling loss, per volumes.bottled_gal)
    for run in BottlingRun.objects.filter(source_lot=lot, voided_at__isnull=True):
        amt = _d(run.volume_bottled_gal)
        loss = run.bottling_loss_gal
        if loss and loss > 0:
            amt += _d(loss)
        items.append({
            "date": _dt(run.bottled_at), "label": "Bottled",
            "detail": "run + bottling loss" if (loss and loss > 0) else "run",
            "increase": None, "decrease": amt.quantize(GAL),
        })

    # − bulk tax-paid removals
    for r in BulkTaxPaidRemoval.objects.filter(lot=lot, voided_at__isnull=True):
        items.append({
            "date": _dt(r.removed_at), "label": "Bulk tax-paid removal",
            "detail": "", "increase": None,
            "decrease": _d(r.wine_gallons).quantize(GAL),
        })

    # − bond transfers out (B2B)
    for t in BondTransfer.objects.filter(
            lot=lot, direction=BondTransfer.Direction.OUT, voided_at__isnull=True):
        items.append({
            "date": _dt(t.transferred_at), "label": "Bond transfer out (B2B)",
            "detail": "", "increase": None, "decrease": _d(t.gallons).quantize(GAL),
        })

    # − must sales
    for s in MustSale.objects.filter(lot=lot, voided_at__isnull=True):
        items.append({
            "date": _dt(s.sold_at), "label": "Must sale", "detail": "",
            "increase": None, "decrease": _d(s.gallons).quantize(GAL),
        })

    # chronological; undated rows sort last, stable within a date
    items.sort(key=lambda r: (r["date"] is None, r["date"] or _dt_min()))

    running = ZERO
    for it in items:
        running += (it["increase"] or ZERO) - (it["decrease"] or ZERO)
        it["balance"] = running.quantize(GAL)

    authoritative = vol.lot_balance(lot)
    reconciles = (authoritative is None and not items) or (
        authoritative is not None and running.quantize(GAL) == authoritative)
    return {"rows": items, "balance": authoritative, "reconciles": reconciles}


def _dt_min():
    import datetime
    return datetime.date.min
