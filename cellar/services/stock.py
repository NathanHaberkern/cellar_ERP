"""
Perpetual weighted-average costing over the consumable stock ledger.

THE ONE RULE
------------
Cost is snapshotted at the moment stock is consumed and never recomputed. A
receipt changes the average going FORWARD only. Nothing that already happened
moves — not when you reprice, not when a count comes up short, not ever.

THE UNIT PROBLEM
----------------
You buy in packs and dose in stock units: tartaric arrives as 4 × 50 lb sacks and
gets dosed in grams; Opti-Red arrives in 1 kg bags and gets dosed in grams. The
catalogs carry a free-text `unit` ("g", "kg", "lb", "mL", "L"), so the receipt
screen takes pack_count × pack_size in a PURCHASE unit and converts once, here,
into the item's stock unit. Everything downstream of a receipt is in stock units
and never has to think about it again.

Conversions are exact where the definition is exact (1 lb = 453.59237 g exactly,
by international agreement). Unknown pairs raise rather than silently assuming
1:1 — a wrong factor is a 453× costing error, which is not something to guess at.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

MONEY = Decimal("0.01")
RATE = Decimal("0.000001")

# canonical base unit per dimension -> factor to base
_MASS = {                       # base: gram
    "g": Decimal("1"), "gram": Decimal("1"), "grams": Decimal("1"),
    "kg": Decimal("1000"), "kilogram": Decimal("1000"),
    "mg": Decimal("0.001"),
    "lb": Decimal("453.59237"), "lbs": Decimal("453.59237"), "pound": Decimal("453.59237"),
    "oz": Decimal("28.349523125"),
    "ton": Decimal("907184.74"),
}
_VOLUME = {                     # base: millilitre
    "ml": Decimal("1"), "millilitre": Decimal("1"), "milliliter": Decimal("1"),
    "l": Decimal("1000"), "litre": Decimal("1000"), "liter": Decimal("1000"),
    "gal": Decimal("3785.411784"), "gallon": Decimal("3785.411784"),
    "hl": Decimal("100000"),
    "floz": Decimal("29.5735295625"),
}
_COUNT = {                      # base: each
    "each": Decimal("1"), "ea": Decimal("1"), "unit": Decimal("1"),
    "case": Decimal("1"), "cs": Decimal("1"), "pack": Decimal("1"),
    "bottle": Decimal("1"), "btl": Decimal("1"),
}
_DIMENSIONS = (_MASS, _VOLUME, _COUNT)


class UnitMismatch(ValueError):
    """Purchase unit and stock unit aren't the same kind of measurement."""


def _norm(u):
    return (u or "").strip().lower().replace(".", "").replace(" ", "")


def convert(qty, from_unit, to_unit):
    """Convert `qty` from one unit to another within the same dimension.

    Identical (or blank/unrecognised-but-equal) units pass straight through, so a
    catalog using a unit this table has never heard of still works as long as the
    receipt is keyed in that same unit.
    """
    f, t = _norm(from_unit), _norm(to_unit)
    q = Decimal(str(qty))
    if f == t:
        return q
    for dim in _DIMENSIONS:
        if f in dim and t in dim:
            return (q * dim[f] / dim[t])
    raise UnitMismatch(
        f"Can't convert {from_unit or '(blank)'} to {to_unit or '(blank)'}. "
        f"They're different kinds of measurement, or one isn't a unit I know. "
        f"Key the receipt in the item's own unit, or fix the item's unit.")


# ---------------------------------------------------------------- item access
def item_filter(item):
    """The StockTransaction kwargs that select this item, whatever catalog it's in."""
    from cellar.models import Additive, DryGood, Material
    if isinstance(item, Additive):
        return {"additive": item}
    if isinstance(item, DryGood):
        return {"dry_good": item}
    if isinstance(item, Material):
        return {"material": item}
    raise TypeError(f"{type(item).__name__} is not a stock-tracked catalog item.")


def _txns(item):
    from cellar.models import StockTransaction
    return StockTransaction.objects.filter(voided_at__isnull=True, **item_filter(item))


def on_hand(item):
    """Book quantity, in the item's stock unit. May be negative — see `issue()`."""
    return _txns(item).aggregate(v=Sum("quantity"))["v"] or Decimal("0")


def on_hand_value(item):
    """Book dollars."""
    return _txns(item).aggregate(v=Sum("extended_cost"))["v"] or Decimal("0")


def wac(item):
    """Current weighted-average unit cost, or None when there's nothing to average.

    Falls back to the catalog's `unit_cost` when the ledger is empty or the book
    has gone non-positive, so an item that has never been received still prices
    an addition instead of silently costing $0.
    """
    qty = on_hand(item)
    if qty and qty > 0:
        return (on_hand_value(item) / qty).quantize(RATE)
    fallback = getattr(item, "unit_cost", None)
    return Decimal(str(fallback)).quantize(RATE) if fallback is not None else None


# ------------------------------------------------------------------- writers
@transaction.atomic
def receive(item, *, pack_count, pack_size, pack_unit, goods_cost,
            freight_cost=0, tax_cost=0, occurred_at=None, supplier="",
            reference="", operator=None, notes=""):
    """Book a purchase. Returns the RECEIPT transaction.

    Quantity is converted from the purchase unit into the item's stock unit here
    and once only. Landed cost = goods + freight + tax, all three capitalized.
    """
    from cellar.models import StockTransaction

    pc, ps = Decimal(str(pack_count)), Decimal(str(pack_size))
    if pc <= 0 or ps <= 0:
        raise ValueError("Pack count and pack size must both be greater than zero.")

    purchased = pc * ps
    qty = convert(purchased, pack_unit or item.unit, item.unit)
    if qty <= 0:
        raise ValueError("Converted quantity came out at zero — check the units.")

    return StockTransaction.objects.create(
        kind=StockTransaction.Kind.RECEIPT,
        occurred_at=occurred_at or timezone.localdate(),
        quantity=qty.quantize(Decimal("0.0001")),
        goods_cost=Decimal(str(goods_cost)),
        freight_cost=Decimal(str(freight_cost or 0)),
        tax_cost=Decimal(str(tax_cost or 0)),
        pack_count=pc, pack_size=ps, pack_unit=pack_unit or item.unit,
        supplier=supplier, reference=reference,
        operator=operator, notes=notes,
        **item_filter(item))


@transaction.atomic
def issue(item, quantity, *, occurred_at=None, addition=None, dry_good_use=None,
          operator=None, notes=""):
    """Consume stock at the CURRENT weighted average. Returns the ISSUE txn, or None.

    Negative on-hand is ALLOWED and flagged, not blocked (Nate's call): the wine
    physically received the addition, so refusing the entry would make the cellar
    record wrong to keep the book tidy. The shortfall surfaces on the next count.

    Backdating does NOT reprice: an addition keyed today for last Tuesday is costed
    at today's average, because that is the average the ledger actually holds. Same
    principle as the LotLineage snapshot — history is never restated.
    """
    from cellar.models import StockTransaction

    qty = Decimal(str(quantity or 0))
    if qty <= 0:
        return None

    rate = wac(item)
    if rate is None:
        rate = Decimal("0")

    return StockTransaction.objects.create(
        kind=StockTransaction.Kind.ISSUE,
        occurred_at=occurred_at or timezone.localdate(),
        quantity=-qty,
        unit_cost=rate,
        extended_cost=(-qty * rate).quantize(MONEY),
        addition=addition, dry_good_use=dry_good_use,
        operator=operator, notes=notes,
        **item_filter(item))


@transaction.atomic
def write_down(item, quantity, *, reason, occurred_at=None, operator=None, notes=""):
    """Expense stock that's gone — expired, spilled, damaged. Never touches a lot."""
    from cellar.models import StockTransaction

    qty = Decimal(str(quantity or 0))
    if qty <= 0:
        raise ValueError("Enter the quantity being written down.")
    if not (reason or "").strip():
        raise ValueError("A write-down needs a reason.")

    rate = wac(item) or Decimal("0")
    return StockTransaction.objects.create(
        kind=StockTransaction.Kind.WRITE_DOWN,
        occurred_at=occurred_at or timezone.localdate(),
        quantity=-qty, unit_cost=rate,
        extended_cost=(-qty * rate).quantize(MONEY),
        reason=reason.strip(), operator=operator, notes=notes,
        **item_filter(item))


@transaction.atomic
def commit_count(count, lines, *, operator=None):
    """Reconcile book to physical. `lines` is [(item, counted_qty), …].

    Writes one COUNT_ADJUSTMENT per item whose book differs from the count, valued
    at the current average, and stamps the session committed. Items counted at
    exactly book get no row — a no-op adjustment is noise in an audit trail.

    Variance dollars are period expense (shrinkage). They are deliberately NOT
    pushed back onto any lot's COGS.
    """
    from cellar.models import StockTransaction

    if count.is_committed:
        raise ValueError(f"{count} was already committed on {count.committed_at:%Y-%m-%d}.")

    written = []
    for item, counted in lines:
        counted = Decimal(str(counted))
        book = on_hand(item)
        delta = (counted - book).quantize(Decimal("0.0001"))
        if delta == 0:
            continue
        rate = wac(item) or Decimal("0")
        written.append(StockTransaction.objects.create(
            kind=StockTransaction.Kind.COUNT_ADJUSTMENT,
            occurred_at=count.counted_on,
            quantity=delta, unit_cost=rate,
            extended_cost=(delta * rate).quantize(MONEY),
            count=count, operator=operator,
            notes=f"book {book} → counted {counted}",
            **item_filter(item)))

    count.committed_at = timezone.now()
    count.save(update_fields=["committed_at"])
    return written


# -------------------------------------------------------------------- report
def stock_report(kinds=("additive", "dry_good", "material"), include_zero=False):
    """On-hand rows for the inventory screen, across all three catalogs."""
    from cellar.models import Additive, DryGood, Material

    catalogs = {"additive": Additive, "dry_good": DryGood, "material": Material}
    rows = []
    for key in kinds:
        model = catalogs.get(key)
        if model is None:
            continue
        for obj in model.objects.order_by("name"):
            qty = on_hand(obj)
            val = on_hand_value(obj)
            last = _txns(obj).order_by("-occurred_at", "-id").first()
            counted = (_txns(obj).filter(kind="count_adj")
                       .order_by("-occurred_at").first())
            if not include_zero and qty == 0 and val == 0 and last is None:
                continue
            rows.append({
                "item": obj, "kind": key,
                "kind_label": {"additive": "Additive", "dry_good": "Dry good",
                               "material": "Material"}[key],
                "unit": obj.unit, "on_hand": qty, "value": val.quantize(MONEY),
                "wac": wac(obj),
                "negative": qty < 0,
                "last_movement": last.occurred_at if last else None,
                "last_counted": counted.occurred_at if counted else None,
            })
    return rows


def total_stock_value(kinds=("additive", "dry_good", "material")):
    return sum((r["value"] for r in stock_report(kinds)), Decimal("0"))
