"""
Seed the canonical lab analytes + ETS name synonyms.

The data and the installer both live in cellar/reference_data.py, which a data
migration also calls — so this command is now a convenience/repair tool, not a
required step. Safe to re-run; adopts pre-existing rows rather than duplicating.

    python manage.py seed_lab_analytes
"""
from django.core.management.base import BaseCommand

from cellar import reference_data
from cellar.models import LabAnalyte, LabAnalyteSynonym


class Command(BaseCommand):
    help = "Seed canonical lab analytes and ETS analysis-name synonyms."

    def handle(self, *args, **opts):
        created, adopted, updated, syns = reference_data.install_analytes(
            LabAnalyte, LabAnalyteSynonym)
        self.stdout.write(self.style.SUCCESS(
            f"Analytes: {created} created, {adopted} adopted, {updated} updated. "
            f"Synonyms: {syns} created. Total now {LabAnalyte.objects.count()}."))
