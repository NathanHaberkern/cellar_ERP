"""
Seed the default auto-task rules. Idempotent — updates name/description in place
and only sets params when the row is first created, so re-running never clobbers
knobs Nate has tuned in the Rules menu.

Run after migrate:  python manage.py seed_task_rules
"""
from django.core.management.base import BaseCommand

from cellar.models import TaskRule

RULES = [
    {
        "key": "topping_interval",
        "name": "Topping interval",
        "description": "Flag lots aging in oak that haven't been topped within "
                       "interval_days. Creates an FSO₂ task and a top-barrels task.",
        "params": {"interval_days": 60},
    },
    {
        "key": "ferment_daily",
        "name": "Fermentation daily cadence",
        "description": "For each lot in one of `statuses`, create a daily cap-"
                       "management task and a daily Brix + temp reading task.",
        "params": {"statuses": ["fermenting"]},
    },
]


class Command(BaseCommand):
    help = "Seed the default auto-task rules."

    def handle(self, *args, **opts):
        made = 0
        for spec in RULES:
            obj, created = TaskRule.objects.get_or_create(
                key=spec["key"],
                defaults={"name": spec["name"], "description": spec["description"],
                          "params": spec["params"], "enabled": True})
            if not created:
                # refresh copy but preserve tuned params + enabled flag
                obj.name = spec["name"]
                obj.description = spec["description"]
                obj.save(update_fields=["name", "description"])
            made += created
        self.stdout.write(self.style.SUCCESS(
            f"Task rules: {made} created, {len(RULES) - made} already present."))
