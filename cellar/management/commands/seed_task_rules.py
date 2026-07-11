"""
Seed the default auto-task rules.

Data + installer live in cellar/reference_data.py, which a data migration also
calls — so this is a convenience/repair command. Re-running never clobbers params
tuned in the Rules menu or a rule you've switched off.

    python manage.py seed_task_rules
"""
from django.core.management.base import BaseCommand

from cellar import reference_data
from cellar.models import TaskRule


class Command(BaseCommand):
    help = "Seed the default auto-task rules."

    def handle(self, *args, **opts):
        created = reference_data.install_task_rules(TaskRule)
        self.stdout.write(self.style.SUCCESS(
            f"Task rules: {created} created. Total now {TaskRule.objects.count()}."))
