"""
Posting engine for the cost ledger.

WHAT POSTING IS
---------------
`costing.py` DERIVES a lot's cost from live objects. This module takes those same
derivations, freezes each one as a CostEntry row against an accounting period, and
never looks at the source again. After a period closes, the posted rows are the
answer — repricing fruit, voiding an addition, or re-running the poster cannot move
a number that has already been reported.

Posting is idempotent. Every row carries (source_kind, source_id, category) under a
unique constraint, so `post_all()` can run nightly, twice in a row, or after a
partial failure, and the ledger lands in the same place.

THE CLOSED-PERIOD RULE
----------------------
An addition keyed in April for a March date, when March is closed, does NOT get
rejected — refusing the cellar entry would teach people to fudge dates, which is
far more expensive than a misfiled dollar. Instead the cost posts to the earliest
OPEN period and the row carries a mandatory `deferred_note` naming the month it
should have landed in. A shifted cost is always visible as a shifted cost.

TRANSFERS ARE POSTED ON BOTH SIDES
----------------------------------
A blend posts a negative TRANSFER_OUT on the parent and a positive TRANSFER_IN on
the child, both at the LotLineage snapshot rate. That is deliberate and it fixes a
real defect: under the live computation a parent's cost was never reduced by the
wine it gave away, so summing every lot double-counted blended wine and overstated
total inventory value. Here, the two rows net to zero across the winery.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

MONEY = Decimal("0.01")


def _d(v):
    return Decimal(str(v or 0)).quantize(MONEY)


# ------------------------------------------------------------------- periods
def period_for(d, create=True):
    """The CostPeriod containing date `d`."""
    from cellar.models import CostPeriod
    if create:
        obj, _ = CostPeriod.objects.get_or_create(year=d.year, month=d.month)
        return obj
    return CostPeriod.objects.filter(year=d.year, month=d.month).first()


def posting_period(d):
    """Where a cost dated `d` actually lands, plus the note if it had to move.

    Returns (period, deferred_note). The note is empty when the natural period was
    open. When it wasn't, we walk forward to the first open month — creating months
    as needed — and the note records where the cost belonged.
    """
    from cellar.models import CostPeriod

    natural = period_for(d)
    if natural.is_open:
        return natural, ""

    later = (CostPeriod.objects.filter(status=CostPeriod.Status.OPEN)
             .filter(models_gte(natural)).order_by("year", "month").first())
    if later is None:
        y, m = natural.year, natural.month
        while True:
            m += 1
            if m > 12:
                m, y = 1, y + 1
            later, _ = CostPeriod.objects.get_or_create(year=y, month=m)
            if later.is_open:
                break
    return later, (f"Event dated {d.isoformat()} belongs in {natural.label}, "
                   f"which was {natural.get_status_display().lower()}. "
                   f"Posted to {later.label} instead.")


def models_gte(period):
    """Q for periods at or after `period` (year/month ordering, not a date column)."""
    from django.db.models import Q
    return (Q(year__gt=period.year) | Q(year=period.year, month__gte=period.month))


@transaction.atomic
def close_period(period, *, operator=None, force=False):
    """Close a month. Refuses if reconciliation is out unless `force`."""
    from cellar.models import CostPeriod
    if period.status != CostPeriod.Status.OPEN:
        raise ValueError(f"{period.label} is already {period.get_status_display().lower()}.")
    if not force:
        bad = [r for r in reconcile() if not r["ok"]]
        if bad:
            raise ValueError(
                f"{len(bad)} lot(s) don't reconcile — posted cost differs from computed. "
                f"Run `manage.py cost_reconcile` and fix, or close with --force. "
                f"First: {bad[0]['lot'].code} posted ${bad[0]['posted']} vs computed ${bad[0]['computed']}.")
    period.status = CostPeriod.Status.CLOSED
    period.closed_at = timezone.now()
    period.save(update_fields=["status", "closed_at"])
    return period


# -------------------------------------------------------------------- posting
def _post(lot, category, amount, occurred_at, source_kind, source_id, operator=None):
    """Insert one CostEntry unless an identical live posting already exists."""
    from cellar.models import CostEntry

    amt = _d(amount)
    if amt == 0:
        return None

    if source_id is not None and CostEntry.objects.filter(
            source_kind=source_kind, source_id=source_id,
            category=category, voided_at__isnull=True).exists():
        return None

    period, note = posting_period(occurred_at)
    return CostEntry.objects.create(
        lot=lot, period=period, category=category, amount=amt,
        occurred_at=occurred_at, source_kind=source_kind, source_id=source_id,
        deferred_note=note, operator=operator)


@transaction.atomic
def post_lot(lot, *, operator=None):
    """Post every unposted direct + transfer cost for one lot. Returns the new rows."""
    from cellar.models import CostEntry, LotLineage
    from cellar.services import costing
    from cellar.services.aging import _lot_volume  # noqa: F401  (parity with costing)

    C = CostEntry.Category
    made = []

    # --- fruit: one row per weigh-tag allocation ---------------------------
    # Price resolution mirrors costing.fruit_cost() exactly (tag cost -> FruitPrice
    # contract -> tag purchase price -> estate constant). Diverging here would show
    # up immediately as a reconciliation failure, which is the point of the check.
    for alloc in lot.allocations.filter(voided_at__isnull=True):
        tag = alloc.weigh_tag
        cpt = tag.fruit_cost_per_ton
        if cpt is None:
            cpt = costing._contract_price(lot, tag)
        if cpt is None:
            cpt = (tag.purchase_price_per_ton if tag.source_type == "purchased"
                   else costing._estate_cost_per_ton())
        tons = Decimal(str(alloc.allocated_net_lbs or 0)) / Decimal("2000")
        made.append(_post(lot, C.FRUIT, tons * Decimal(str(cpt or 0)),
                          costing.to_business_date(getattr(tag, "received_at", None))
                          or costing.to_business_date(lot.created_at),
                          "weightagallocation", alloc.pk, operator))

    # --- fruit true-up: the delta between provisional and final price ------
    # Posted as its own row against the SAME allocation under a different
    # source_kind, so the unique constraint treats it as a distinct posting and the
    # original fruit row is never touched. Dated to the revision's effective_on —
    # the day the final figure published — not the delivery date. The harvest month
    # is always closed by then, so posting_period() moves it forward to the open
    # month and stamps a deferred_note. That is correct and it is the point: the
    # true-up is a new fact learned in March, not a restatement of September.
    #
    # CORRECTING A TRUE-UP: because idempotency keys on (source_kind, source_id,
    # category), voiding a revision and entering a corrected one will NOT repost —
    # the first true-up row is still live and blocks it. Void the CostEntry rows too.
    # reconcile() catches this on its own (posted delta vs. recomputed delta) and
    # close_period() refuses, so it fails loudly rather than silently.
    for alloc, delta, rev in costing.trueup_allocations(lot):
        tons = Decimal(str(alloc.allocated_net_lbs or 0)) / Decimal("2000")
        made.append(_post(lot, C.FRUIT, tons * Decimal(str(delta)),
                          rev.effective_on,
                          "weightagallocation_trueup", alloc.pk, operator))

    # --- additives ---------------------------------------------------------
    for a in lot.additions.filter(voided_at__isnull=True):
        made.append(_post(lot, C.ADDITIVE, a.cost,
                          costing.to_business_date(a.added_at),
                          "addition", a.pk, operator))

    # --- spirit ------------------------------------------------------------
    for f in lot.fortifications.filter(voided_at__isnull=True):
        made.append(_post(lot, C.SPIRIT, f.spirit_cost,
                          costing.to_business_date(getattr(f, "fortified_at", None))
                          or costing.to_business_date(f.booked_at),
                          "fortificationevent", f.pk, operator))

    # --- oak: the whole computed slice, keyed to the lot -------------------
    oak = costing.lot_oak_depreciation(lot)
    if oak:
        made.append(_post(lot, C.OAK, oak, _oak_date(lot), "lot_oak", lot.pk, operator))

    # --- manual adjustments ------------------------------------------------
    for adj in lot.cost_adjustments.filter(voided_at__isnull=True):
        made.append(_post(lot, C.ADJUSTMENT, adj.amount, adj.incurred_at,
                          "lotcostadjustment", adj.pk, operator))

    # --- transfers: both sides, at the frozen snapshot rate ----------------
    for e in LotLineage.objects.filter(parent_lot=lot, voided_at__isnull=True):
        if e.cost_per_gal_snapshot is None or not e.volume_gal:
            continue
        moved = _d(e.cost_per_gal_snapshot * e.volume_gal)
        when = e.occurred_at or costing.to_business_date(e.created_at)
        made.append(_post(lot, C.TRANSFER_OUT, -moved, when, "lotlineage_out", e.pk, operator))
        made.append(_post(e.child_lot, C.TRANSFER_IN, moved, when,
                          "lotlineage_in", e.pk, operator))

    return [m for m in made if m is not None]


def _oak_date(lot):
    """Date to book a lot's oak slice against: its last barrel departure, else today."""
    from cellar.models import AgingPlacement
    from cellar.services import costing
    p = (AgingPlacement.objects.filter(lot=lot, voided_at__isnull=True)
         .order_by("-placed_at").first())
    if p is None:
        return timezone.localdate()
    return (costing.to_business_date(getattr(p, "removed_at", None))
            or costing.to_business_date(p.placed_at)
            or timezone.localdate())


@transaction.atomic
def post_all(*, lots=None, operator=None):
    """Post every lot. Returns {'entries': n, 'lots': n, 'deferred': n}."""
    from cellar.models import Lot
    qs = lots if lots is not None else Lot.objects.all()
    made, touched = [], 0
    for lot in qs:
        rows = post_lot(lot, operator=operator)
        if rows:
            touched += 1
            made.extend(rows)
    return {"entries": len(made), "lots": touched,
            "deferred": sum(1 for r in made if r.deferred_note)}


# ------------------------------------------------------------- reading & recon
def lot_cost_posted(lot):
    """A lot's cost from the ledger. Float, to match costing.lot_cost()."""
    from django.db.models import Sum
    # No category exclusion here, deliberately. Period-expense rows (shrinkage, idle
    # capacity, and the expense half of an abnormal loss) all carry lot=None, so the
    # FK filter already excludes them. Excluding by CATEGORY as well would swallow the
    # abnormal-loss CREDIT — the negative row that is supposed to take the destroyed
    # wine's cost back OUT of the lot.
    v = (lot.cost_entries.filter(voided_at__isnull=True)
         .aggregate(v=Sum("amount"))["v"])
    return float(v or 0)


def has_postings(lot):
    return lot.cost_entries.filter(voided_at__isnull=True).exists()


def reconcile(lots=None, tolerance=Decimal("0.05")):
    """Posted vs. freshly computed, per lot. The drift check a stored ledger needs.

    TRANSFER_OUT is the one expected difference: the posted ledger reduces a parent
    by wine it gave away, and costing.lot_cost_computed() does not. So the comparison adds transfers-out back before
    diffing — otherwise every blended parent would look broken.
    """
    from django.db.models import Sum
    from cellar.models import Lot
    from cellar.services import costing

    qs = lots if lots is not None else Lot.objects.all()
    out = []
    for lot in qs:
        if not has_postings(lot):
            continue
        posted = Decimal(str(lot_cost_posted(lot)))
        transferred_out = (lot.cost_entries.filter(
            voided_at__isnull=True, category="transfer_out")
            .aggregate(v=Sum("amount"))["v"] or Decimal("0"))
        computed = Decimal(str(costing.lot_cost_computed(lot)))
        diff = (posted - transferred_out) - computed
        out.append({
            "lot": lot, "posted": posted.quantize(MONEY),
            "computed": computed.quantize(MONEY),
            "transferred_out": transferred_out.quantize(MONEY),
            "diff": diff.quantize(MONEY), "ok": abs(diff) <= tolerance,
        })
    return out


def period_summary(period):
    """Category totals for a period — the shape a QBO journal entry needs."""
    from django.db.models import Sum
    from cellar.models import CostEntry

    rows = (CostEntry.objects.filter(period=period, voided_at__isnull=True)
            .values("category").annotate(total=Sum("amount")).order_by("category"))
    labels = dict(CostEntry.Category.choices)
    capitalized = Decimal("0")
    expense = Decimal("0")
    lines = []
    for r in rows:
        amt = r["total"] or Decimal("0")
        is_exp = r["category"] in CostEntry.EXPENSE_CATEGORIES
        lines.append({"category": r["category"], "label": labels.get(r["category"], r["category"]),
                      "total": amt.quantize(MONEY), "is_expense": is_exp})
        if is_exp:
            expense += amt
        else:
            capitalized += amt
    return {"period": period, "lines": lines,
            "capitalized": capitalized.quantize(MONEY),
            "expense": expense.quantize(MONEY),
            "deferred": CostEntry.objects.filter(period=period, voided_at__isnull=True)
                        .exclude(deferred_note="").count()}


def wip_total():
    """Total capitalized cost sitting in unbottled lots — ties to the QBO WIP balance."""
    from cellar.models import Lot
    total = Decimal("0")
    for lot in Lot.objects.all():
        if has_postings(lot):
            total += Decimal(str(lot_cost_posted(lot)))
    return total.quantize(MONEY)
