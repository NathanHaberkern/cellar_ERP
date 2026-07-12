"""
Auto-task rule catalog.

Each rule is a function (rule: TaskRule, today: date) -> int (tasks created). The
`TaskRule` row carries the tunable parameters and the on/off switch; the logic
lives here, keyed by rule.key. Idempotency is by dedupe_key: a key encodes enough
state that a rule re-fires only when the situation genuinely changes —
  * daily rules key on the date, so one runs per lot per day;
  * the topping rule keys on the last-topped date, so it re-arms only after a new
    topping resets the clock.

The Fermaid-O-from-plan rule is NOT here: those tasks are generated at inoculation
(slice C) by calling tasks.create_task directly with the nutrition plan, so they
carry the planned dose to confirm on completion. This module is the nightly
state-scanner; that one is event-driven.

Entry point: run_all(today) — called by the run_task_rules management command.
"""
from datetime import timedelta

from django.utils import timezone

from cellar.models import Lot, TaskRule, AgingPlacement, ToppingTarget
from cellar.services import tasks as task_svc


def _today():
    return timezone.localdate()


# --------------------------------------------------------- topping interval
def rule_topping_interval(rule, today):
    """Lots aging in oak that haven't been topped within interval_days get two
    tasks: run an FSO₂, then top the barrels."""
    days = int(rule.params.get("interval_days", 60))
    cutoff = today - timedelta(days=days)
    made = 0

    # lots currently holding wine in an oak container
    lots = {}
    for p in (AgingPlacement.objects
              .filter(emptied_at__isnull=True, voided_at__isnull=True)
              .select_related("container", "lot")):
        if getattr(p.container, "is_oak", False):
            lots.setdefault(p.lot_id, p.lot)

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

        state = last_date.isoformat()
        _, c1 = task_svc.create_task(
            title=f"Run FSO₂ — {lot.code}",
            body=f"{lot.code} last topped {last_date} (> {days} days). "
                 f"Pull free SO₂ before topping.",
            due_date=today, lot=lot, rule=rule,
            dedupe_key=f"topfso2:{lot_id}:{state}")
        _, c2 = task_svc.create_task(
            title=f"Top barrels — {lot.code}",
            body=f"{lot.code} last topped {last_date} (> {days} days).",
            due_date=today, lot=lot, rule=rule,
            dedupe_key=f"toptop:{lot_id}:{state}")
        made += int(c1) + int(c2)
    return made


# ------------------------------------------------------------- ferment daily
def rule_ferment_daily(rule, today):
    """Each fermenting lot gets a daily cap-management task (punch-down / pump-over)
    and a daily Brix + temperature reading task."""
    statuses = rule.params.get("statuses") or [Lot.Status.FERMENTING]
    d = today.isoformat()
    made = 0
    for lot in Lot.objects.filter(status__in=statuses):
        _, c1 = task_svc.create_task(
            title=f"Cap management — {lot.code}",
            body="Punch-down or pump-over, then log it.",
            due_date=today, lot=lot, rule=rule, dedupe_key=f"fermcap:{lot.pk}:{d}")
        _, c2 = task_svc.create_task(
            title=f"Brix + temp — {lot.code}",
            body="Take today's Brix and temperature reading.",
            due_date=today, lot=lot, rule=rule, dedupe_key=f"fermread:{lot.pk}:{d}")
        made += int(c1) + int(c2)
    return made


# ------------------------------------------------------- partial barrel fill
def rule_partial_barrel(rule, today):
    """Barrels that came off the fill line short.

    Filling barrels from a tank almost always ends on a partial. That barrel is
    not empty — it holds wine and the container is spoken for — but it is not full
    either, and wine sitting under that much headspace oxidises. Flag it and open a
    task to bring it up to full from another lot.

    Keyed on the placement, so it fires once per partial barrel and does not
    re-fire while the barrel stays partial. Filling it makes it no longer partial,
    so the rule stops selecting it; topping.top_partial() closes the open task.
    """
    from cellar.services import volumes as vol_svc

    grace = int(rule.params.get("grace_days", 0))
    made = 0
    for p in (AgingPlacement.objects
              .filter(emptied_at__isnull=True, voided_at__isnull=True)
              .select_related("container", "lot")):
        if not getattr(p.container, "is_oak", False):
            continue
        if not vol_svc.is_partial(p):
            continue
        if grace and (today - p.filled_at).days < grace:
            continue
        room = vol_svc.ullage(p)
        _, created = task_svc.create_task(
            title=f"Fill partial barrel {p.container.container_id} — {p.lot.code}",
            body=f"{p.container.container_id} was filled to "
                 f"{vol_svc.placement_volume(p)} gal of "
                 f"{vol_svc.placement_capacity(p)} gal working capacity. "
                 f"{room} gal of ullage — top it up from another lot.",
            due_date=today, lot=p.lot, container=p.container, rule=rule,
            dedupe_key=f"partialbbl:{p.pk}")
        made += int(created)
    return made


RULES = {
    "topping_interval": rule_topping_interval,
    "ferment_daily": rule_ferment_daily,
    "partial_barrel": rule_partial_barrel,
}


def run_all(today=None):
    """Evaluate every enabled rule. Returns {rule_key: tasks_created}."""
    today = today or _today()
    summary = {}
    for rule in TaskRule.objects.filter(enabled=True):
        fn = RULES.get(rule.key)
        if fn is None:
            continue
        summary[rule.key] = fn(rule, today)
    return summary
