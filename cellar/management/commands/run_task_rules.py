"""
Evaluate all enabled auto-task rules and create any missing tasks.

Idempotent (dedupe_key), so it's safe to run repeatedly. Wire it to Heroku
Scheduler to run once daily; locally, run by hand:

    python manage.py run_task_rules
"""
from django.core.management.base import BaseCommand

from cellar.services import taskrules


class Command(BaseCommand):
    help = "Run auto-task rules and create any missing tasks."

    def handle(self, *args, **opts):
        summary = taskrules.run_all()
        total = sum(summary.values())
        for key, n in summary.items():
            self.stdout.write(f"  {key}: {n} created")
        self.stdout.write(self.style.SUCCESS(f"Done — {total} task(s) created."))
