"""
Daily Checklist + Daily Plan.

Checklist: cross-lot view of today's fermentation housekeeping (cap
management, Brix/temp readings) — one screen instead of clicking into every
fermenting lot to check whether it's been done. Status is read directly from
the ledger (Reading / PumpOverEvent / PunchDownEvent for today), not from
Task rows — so it's accurate even before tonight's task-rule run, same
philosophy as cellar_board.py.

Plan: a single editable DailyPlan row per date. The first time a date is
opened, it's auto-drafted from live data (press/barrel-down estimates from
services.fermentation, fruit still on a weigh tag). After that it's edited
directly — checked off, added to, pruned — and `add_suggestions` only appends
items not already present (by auto_key), so a manual edit is never clobbered.
"""
import uuid
from datetime import timedelta

from django.utils import timezone

from cellar.models import (Lot, WeighTag, Reading, PumpOverEvent, PunchDownEvent,
                            AgingPlacement, DailyPlan)
from cellar.services import fermentation as ferm_svc
from cellar.services import dashboard_mode

CHECKLIST_STATUSES = (Lot.Status.COLD_SOAK, Lot.Status.FERMENTING)


# --------------------------------------------------------------- checklist
def checklist_rows(today=None):
    """One row per lot currently fermenting/cold-soaking: has today's Brix,
    temp, and cap-management (pumpover or punchdown) been logged."""
    today = today or timezone.localdate()
    rows = []
    for lot in Lot.objects.filter(status__in=CHECKLIST_STATUSES).order_by("pk"):
        brix_today = (Reading.objects.filter(lot=lot, analyte=Reading.Analyte.BRIX,
                                             voided_at__isnull=True,
                                             measured_at__date=today).exists())
        temp_today = (Reading.objects.filter(lot=lot, analyte=Reading.Analyte.TEMP,
                                             voided_at__isnull=True,
                                             measured_at__date=today).exists())
        cap_today = (PumpOverEvent.objects.filter(lot=lot, voided_at__isnull=True,
                                                   started_at__date=today).exists()
                     or PunchDownEvent.objects.filter(lot=lot, voided_at__isnull=True,
                                                       occurred_at__date=today).exists())
        latest_brix = (Reading.objects.filter(lot=lot, analyte=Reading.Analyte.BRIX,
                                              voided_at__isnull=True)
                       .order_by("-measured_at").first())
        rows.append({
            "lot": lot, "brix_done": brix_today, "temp_done": temp_today,
            "cap_done": cap_today,
            "latest_brix": latest_brix.value if latest_brix else None,
        })
    return rows


# ------------------------------------------------------------- plan drafting
def _auto_item(category, label, detail, lot=None, auto_key=None):
    return {
        "id": uuid.uuid4().hex[:12], "category": category, "label": label,
        "detail": detail, "lot_id": lot.pk if lot else None,
        "lot_code": lot.code if lot else None,
        "done": False, "source": "auto", "auto_key": auto_key,
    }


def draft_items(today=None):
    """Fresh auto-suggested items from current data. Pure computation — does
    not touch the database beyond reads, and does not know about any
    existing DailyPlan; callers merge/dedupe by auto_key."""
    today = today or timezone.localdate()
    tomorrow = today + timedelta(days=1)
    items = []

    for lot in Lot.objects.filter(status__in=dashboard_mode.CRUSH_STATUSES):
        est = ferm_svc.estimate_press_and_barrel_dates(lot, asof=today)
        press_date = est["press_date"]
        barrel_date = est["barrel_down_date"]

        if press_date and press_date <= today and lot.status != Lot.Status.PRESSED:
            overdue = " (overdue)" if press_date < today else ""
            items.append(_auto_item(
                "press", f"Press {lot.code}", f"est. {press_date}{overdue}",
                lot=lot, auto_key=f"press:{lot.pk}:{press_date}"))

        if barrel_date:
            has_oak = AgingPlacement.objects.filter(
                lot=lot, emptied_at__isnull=True, voided_at__isnull=True).exists()
            if barrel_date <= today and not has_oak:
                overdue = " (overdue)" if barrel_date < today else ""
                items.append(_auto_item(
                    "barrel_down", f"Barrel down {lot.code}", f"est. {barrel_date}{overdue}",
                    lot=lot, auto_key=f"barreldown:{lot.pk}:{barrel_date}"))
            elif barrel_date == tomorrow and not has_oak:
                items.append(_auto_item(
                    "barrel_prep", f"Steam/soak barrels for {lot.code}",
                    f"est. barrel-down tomorrow ({barrel_date})",
                    lot=lot, auto_key=f"barrelprep:{lot.pk}:{barrel_date}"))

    for wt in WeighTag.objects.filter(disposition=WeighTag.Disposition.CRUSHED):
        remaining = wt.remaining_lbs
        if remaining and remaining > 0:
            items.append(_auto_item(
                "fruit", f"Process remaining fruit — tag {wt.weigh_tag_number}",
                f"{remaining:g} lb left",
                auto_key=f"fruit:{wt.pk}:{today}"))

    return items


def get_or_create_plan(today=None):
    """The editable plan for a date — auto-drafted on first creation only.
    Subsequent loads return the same row untouched; use add_suggestions to
    pull in fresh auto items without disturbing manual edits."""
    today = today or timezone.localdate()
    plan, created = DailyPlan.objects.get_or_create(date=today)
    if created:
        plan.items = draft_items(today)
        plan.save(update_fields=["items"])
    return plan


def add_suggestions(plan):
    """Append fresh auto-drafted items not already present (by auto_key).
    Never touches or removes existing items — manual edits and completed
    items are always preserved."""
    existing_keys = {i.get("auto_key") for i in plan.items if i.get("auto_key")}
    fresh = draft_items(plan.date)
    added = [i for i in fresh if i["auto_key"] not in existing_keys]
    if added:
        plan.items = plan.items + added
        plan.save(update_fields=["items"])
    return len(added)


def toggle_item(plan, item_id):
    for i in plan.items:
        if i["id"] == item_id:
            i["done"] = not i["done"]
            break
    plan.save(update_fields=["items"])


def add_manual_item(plan, category, label, detail=""):
    label = (label or "").strip()
    if not label:
        return
    plan.items = plan.items + [{
        "id": uuid.uuid4().hex[:12], "category": category, "label": label,
        "detail": (detail or "").strip(), "lot_id": None, "lot_code": None,
        "done": False, "source": "manual", "auto_key": None,
    }]
    plan.save(update_fields=["items"])


def remove_item(plan, item_id):
    plan.items = [i for i in plan.items if i["id"] != item_id]
    plan.save(update_fields=["items"])


CATEGORIES = [
    ("press", "Press schedule"),
    ("barrel_down", "Barrel down"),
    ("barrel_prep", "Barrel prep (steam/soak)"),
    ("fruit", "Fruit processing"),
    ("other", "Other"),
]


def grouped_items(plan):
    """items bucketed by category, in the fixed CATEGORIES display order."""
    by_cat = {key: [] for key, _ in CATEGORIES}
    for i in plan.items:
        by_cat.setdefault(i.get("category", "other"), []).append(i)
    return [{"key": key, "label": label, "items": by_cat.get(key, [])}
            for key, label in CATEGORIES]
