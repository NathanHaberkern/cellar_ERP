"""
Monthly overhead allocation.

THE MECHANIC
------------
For each pool with dollars entered for a month:

    absorbing gallons = Σ bulk gallons at month end, over lots young enough to absorb
    normal capacity   = ConfigConstant `normal_capacity_gal`

    absorbed = pool × min(1, absorbing / normal)
    idle     = pool − absorbed                       -> expensed, no lot

    each lot's share = absorbed × (its gallons / absorbing gallons)

Gallons are measured with `volumes.lot_balance(lot, as_of=month_end)`, which is a
real as-of query, not today's balance. Allocating October a week into November
therefore gives the same answer in November as it will next March — which is the
whole point of a period-locked ledger.

WHY IDLE CAPACITY IS EXPENSED
-----------------------------
If you crush a light vintage and spread the full year's cellar overhead over the
gallons that happen to exist, that wine looks expensive for reasons that have
nothing to do with the wine. ASC 330 says the unabsorbed portion of fixed overhead
in a below-normal period is a period cost. `barrel_depreciation_by_lot()` already
takes this position for empty barrel-years; this applies it to the pools.

Absorption is capped at 1.0 — a heavy vintage never loads MORE than the pool.

WHY OLD LOTS STOP ABSORBING
---------------------------
`overhead_absorption_max_years` (default 3). A 2014 Port left in the denominator
would collect a slice of every monthly pool for over a decade and end up carried
above its realisable value, which forces an NRV write-down. It still occupies
barrels; it just stops picking up cellar labour and utilities.

IDEMPOTENCY
-----------
Allocation posts many CostEntry rows from ONE OverheadPoolPeriod, so the ledger's
unique key (source_kind, source_id, category) can't be used the obvious way —
source_id has to vary per row. The key used is:

    source_kind = f"pool:{poolperiod.pk}"   source_id = lot.pk

which is unique per (pool-month, lot, category) inside the existing constraint and
needs no schema change. `allocated_at` on the pool-period is the second guard:
allocating twice raises rather than double-posting.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

MONEY = Decimal("0.01")
GAL = Decimal("0.1")

DEFAULT_POOLS = [
    ("production-labor", "Production labor", "labor", 10),
    ("cellar-overhead", "Cellar overhead", "overhead", 20),
    ("barrel-depreciation", "Barrel depreciation", "overhead", 30),
    ("utilities", "Utilities", "overhead", 40),
    ("production-supplies", "Production supplies", "overhead", 50),
    ("equipment-depreciation", "Equipment depreciation", "overhead", 60),
]


# ------------------------------------------------------------------- settings
def _config(key, default):
    from cellar.models import ConfigConstant
    row = ConfigConstant.objects.filter(key=key).first()
    if row is None:
        return Decimal(str(default))
    try:
        return Decimal(str(row.value))
    except Exception:
        return Decimal(str(default))


def normal_capacity_gal():
    """Gallons the cellar is built to carry. Below this, overhead goes idle."""
    return _config("normal_capacity_gal", 60000)


def absorption_max_years():
    return int(_config("overhead_absorption_max_years", 3))


def month_end(period):
    import calendar
    import datetime as dt
    return dt.date(period.year, period.month,
                   calendar.monthrange(period.year, period.month)[1])


# ------------------------------------------------------------------- gallons
def absorbing_lots(period):
    """[(lot, gallons)] for bulk lots that absorb this month's overhead.

    Excluded: lots with no balance, lots bottled out (balance <= 0), bottling
    parcels (already out of bulk WIP), and lots older than the absorption cap.
    """
    from cellar.models import Lot, LotKind
    from cellar.services import volumes as vol

    as_of = month_end(period)
    cutoff_vintage = period.year - absorption_max_years()

    rows = []
    for lot in Lot.objects.all():
        if getattr(lot, "kind", None) == LotKind.BOTTLING:
            continue
        if (lot.vintage_year or 0) <= cutoff_vintage:
            continue
        bal = vol.lot_balance(lot, as_of=as_of)
        if bal is None or bal <= 0:
            continue
        rows.append((lot, Decimal(str(bal))))
    return rows


# ---------------------------------------------------------------- allocation
def preview(period):
    """What allocating this period would do. No writes."""
    from cellar.models import OverheadPoolPeriod

    lots = absorbing_lots(period)
    total_gal = sum((g for _, g in lots), Decimal("0"))
    normal = normal_capacity_gal()
    ratio = min(Decimal("1"), (total_gal / normal)) if normal > 0 else Decimal("1")

    pools = []
    for pp in (OverheadPoolPeriod.objects
               .filter(period=period, voided_at__isnull=True)
               .select_related("pool", "period")):
        absorbed = (pp.amount * ratio).quantize(MONEY)
        pools.append({
            "pool_period": pp, "pool": pp.pool, "amount": pp.amount,
            "absorbed": absorbed, "idle": (pp.amount - absorbed).quantize(MONEY),
            "already": pp.is_allocated,
        })
    return {
        "period": period, "as_of": month_end(period),
        "lots": lots, "lot_count": len(lots),
        "absorbing_gallons": total_gal.quantize(GAL),
        "normal_capacity": normal, "absorption_ratio": ratio,
        "pools": pools,
        "total_amount": sum((p["amount"] for p in pools), Decimal("0")),
        "total_absorbed": sum((p["absorbed"] for p in pools), Decimal("0")),
        "total_idle": sum((p["idle"] for p in pools), Decimal("0")),
    }


@transaction.atomic
def allocate(period, *, operator=None):
    """Post this month's pools across absorbing lots. Idempotent per pool-month."""
    from cellar.models import CostEntry
    from cellar.services import cost_ledger

    plan = preview(period)
    if not plan["pools"]:
        raise ValueError(f"No pool amounts entered for {period.label}.")

    total_gal = plan["absorbing_gallons"]
    made = []

    for p in plan["pools"]:
        pp = p["pool_period"]
        if pp.is_allocated:
            continue

        if total_gal <= 0:
            # Nothing to absorb into: the entire pool is idle.
            absorbed, idle = Decimal("0"), pp.amount
        else:
            absorbed, idle = p["absorbed"], p["idle"]
            running = Decimal("0")
            key = f"pool:{pp.pk}"
            for i, (lot, gal) in enumerate(plan["lots"]):
                if i == len(plan["lots"]) - 1:
                    share = absorbed - running        # last lot eats the rounding
                else:
                    share = (absorbed * gal / total_gal).quantize(MONEY)
                    running += share
                row = cost_ledger._post(lot, pp.pool.category, share,
                                        plan["as_of"], key, lot.pk, operator)
                if row:
                    made.append(row)

        if idle:
            row = cost_ledger._post(None, CostEntry.Category.IDLE_CAPACITY, idle,
                                    plan["as_of"], f"idle:{pp.pk}", pp.pk, operator)
            if row:
                made.append(row)

        pp.allocated_at = timezone.now()
        pp.absorbed_amount = absorbed
        pp.idle_amount = idle
        pp.absorbing_gallons = total_gal
        pp.save(update_fields=["allocated_at", "absorbed_amount",
                               "idle_amount", "absorbing_gallons"])

    return {"entries": len(made), "absorbed": plan["total_absorbed"],
            "idle": plan["total_idle"], "lots": plan["lot_count"]}


# ------------------------------------------------------------ abnormal losses
@transaction.atomic
def post_abnormal_losses(*, operator=None):
    """Expense flagged losses out of the lots that suffered them.

    Posts a NEGATIVE ABNORMAL_LOSS row against the lot (crediting its cost) and a
    POSITIVE one with no lot (the period expense). The pair nets to zero, so total
    dollars in the system are unchanged — the cost simply stops being inventory.

    Value is the lot's cost per gallon at the time of posting, times the gallons lost.
    """
    from cellar.models import CostEntry, VolumeLoss
    from cellar.services import cost_ledger, costing

    made = []
    for loss in (VolumeLoss.objects.filter(is_abnormal=True, voided_at__isnull=True)
                 .select_related("lot")):
        cpg = costing.lot_cost_per_gal(loss.lot)
        if not cpg:
            continue
        value = (Decimal(str(cpg)) * Decimal(str(loss.volume_gal))).quantize(MONEY)
        if value <= 0:
            continue
        a = cost_ledger._post(loss.lot, CostEntry.Category.ABNORMAL_LOSS, -value,
                              loss.occurred_at, "volumeloss_credit", loss.pk, operator)
        b = cost_ledger._post(None, CostEntry.Category.ABNORMAL_LOSS, value,
                              loss.occurred_at, "volumeloss_expense", loss.pk, operator)
        made.extend([r for r in (a, b) if r])
    return made


# ------------------------------------------------------------------- seeding
def ensure_default_pools():
    from cellar.models import OverheadPool
    created = []
    for key, name, category, order in DEFAULT_POOLS:
        obj, was_new = OverheadPool.objects.get_or_create(
            key=key, defaults={"name": name, "category": category, "sort_order": order})
        if was_new:
            created.append(obj)
    return created
