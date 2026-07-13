"""
Cellar Mode board — read-only aggregation for the summer/topping-season
dashboard (see dashboard_mode.py for when this shows instead of the tank map).

Three panels, per Nate's spec: what needs topping/SO2, which barrels are
sitting partial, and which lots need VA watched. All three are pure reads —
nothing here creates a Task or writes to the ledger; `taskrules.py` already
owns task creation (topping_interval, partial_barrel) and this deliberately
mirrors its selection logic rather than reading Task rows, so the board stays
accurate even if a task was dismissed/reassigned/hasn't run tonight yet.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from cellar.models import AgingPlacement, ToppingTarget, ConfigConstant
from cellar.models.fermentation import LabResultValue
from cellar.services import volumes as vol_svc

VA_ANALYTE_SLUG = "va"
DEFAULT_TOPPING_INTERVAL_DAYS = 60
DEFAULT_VA_HIGH_GL = Decimal("0.90")
DEFAULT_VA_RISING_DELTA_GL = Decimal("0.10")


def _config_decimal(key, default):
    row = ConfigConstant.objects.filter(key=key).first()
    if not row:
        return default
    try:
        return Decimal(row.value)
    except Exception:  # noqa: BLE001
        return default


def _config_int(key, default):
    row = ConfigConstant.objects.filter(key=key).first()
    if not row:
        return default
    try:
        return int(row.value)
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------- topping/SO2
def topping_due(today=None):
    """Lots aging in oak that haven't been topped within the configured
    interval — same selection as taskrules.rule_topping_interval, as a read.

    `interval_days` comes from ConfigConstant key 'topping_interval_days' if
    set (kept in sync with the TaskRule's own params by convention, not by a
    hard link — the rule is the source of truth for what actually creates
    tasks; this reads a plain default so the board works even if that rule
    row is disabled).
    """
    today = today or timezone.localdate()
    days = _config_int("topping_interval_days", DEFAULT_TOPPING_INTERVAL_DAYS)
    cutoff = today - timedelta(days=days)

    lots = {}
    for p in (AgingPlacement.objects
              .filter(emptied_at__isnull=True, voided_at__isnull=True)
              .select_related("container", "lot")):
        if getattr(p.container, "is_oak", False):
            lots.setdefault(p.lot_id, p.lot)

    rows = []
    for lot_id, lot in lots.items():
        last = (ToppingTarget.objects
                .filter(placement__lot_id=lot_id, voided_at__isnull=True)
                .select_related("event")
                .order_by("-event__topped_at").first())
        if last is not None:
            last_date = last.event.topped_at
        else:
            fp = (AgingPlacement.objects.filter(lot_id=lot_id, voided_at__isnull=True)
                  .order_by("filled_at").first())
            last_date = fp.filled_at if fp else None
        if last_date is None or last_date > cutoff:
            continue
        barrel_count = (AgingPlacement.objects
                         .filter(lot_id=lot_id, emptied_at__isnull=True, voided_at__isnull=True)
                         .count())
        rows.append({
            "lot": lot, "last_topped": last_date,
            "days_since": (today - last_date).days,
            "barrel_count": barrel_count,
        })
    rows.sort(key=lambda r: -r["days_since"])
    return rows


# ------------------------------------------------------------- partial fill
def partial_fill_barrels():
    """Every open oak placement sitting short of capacity — the same
    condition taskrules.rule_partial_barrel flags, read directly."""
    rows = []
    for p in (AgingPlacement.objects
              .filter(emptied_at__isnull=True, voided_at__isnull=True)
              .select_related("container", "lot")):
        if not getattr(p.container, "is_oak", False):
            continue
        if not vol_svc.is_partial(p):
            continue
        rows.append({
            "lot": p.lot,
            "container_id": p.container.container_id,
            "volume_gal": vol_svc.placement_volume(p),
            "capacity_gal": vol_svc.placement_capacity(p),
            "ullage_gal": vol_svc.ullage(p),
            "filled_at": p.filled_at,
            "days_since_fill": (timezone.localdate() - p.filled_at).days,
        })
    rows.sort(key=lambda r: -r["ullage_gal"])
    return rows


# ------------------------------------------------------------------ VA watch
def va_watch():
    """Lots whose latest VA is high, or rising meaningfully since the prior
    reading — 'monitoring for VA that is creeping up or already high'.

    Thresholds are ConfigConstants ('va_watch_high_gl', 'va_watch_rising_gl')
    so they can be tuned from the Reference screen without a code change;
    defaults are conservative starting points, not a substitute for your own
    winemaking judgment on when a lot needs attention.

    Scoped to lots currently holding bulk wine (an open oak or tank
    placement) — a bottled lot's VA history isn't a cellar action item.
    """
    high = _config_decimal("va_watch_high_gl", DEFAULT_VA_HIGH_GL)
    rising = _config_decimal("va_watch_rising_gl", DEFAULT_VA_RISING_DELTA_GL)

    active_lot_ids = set(
        AgingPlacement.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("lot_id", flat=True))
    from cellar.models import TankAssignment
    active_lot_ids |= set(
        TankAssignment.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("lot_id", flat=True))
    if not active_lot_ids:
        return []

    values = (LabResultValue.objects
              .filter(result__lot_id__in=active_lot_ids,
                      analyte__slug=VA_ANALYTE_SLUG,
                      voided_at__isnull=True, result__voided_at__isnull=True)
              .select_related("result", "result__lot")
              .order_by("result__lot_id", "-result__reported_at"))

    by_lot = {}
    for v in values:
        by_lot.setdefault(v.result.lot_id, []).append(v)

    rows = []
    for lot_id, readings in by_lot.items():
        latest = readings[0]
        prior = readings[1] if len(readings) > 1 else None
        latest_va = Decimal(str(latest.value))
        prior_va = Decimal(str(prior.value)) if prior else None
        delta = (latest_va - prior_va) if prior_va is not None else None

        is_high = latest_va >= high
        is_rising = delta is not None and delta >= rising
        if not (is_high or is_rising):
            continue
        reason = "high & rising" if (is_high and is_rising) else ("high" if is_high else "rising")
        rows.append({
            "lot": latest.result.lot,
            "latest_va": latest_va,
            "prior_va": prior_va,
            "delta": delta,
            "reported_at": latest.result.reported_at,
            "reason": reason,
        })
    rows.sort(key=lambda r: -r["latest_va"])
    return rows


def board_context():
    """Everything the Cellar Mode template needs, in one call."""
    return {
        "topping_due": topping_due(),
        "partial_barrels": partial_fill_barrels(),
        "va_watch": va_watch(),
    }
