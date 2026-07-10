"""
Place the persistent vessels on the dashboard tank map (idempotent, place-only).

seed_reference already calls this same placement, so you normally don't need to
run it separately — it's here for re-placing after a manual vessel edit.

    python manage.py seed_vessel_layout
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from cellar.web.tankmap import place_persistent_vessels


class Command(BaseCommand):
    help = "Place the persistent vessels on the dashboard tank map (idempotent, place-only)."

    @transaction.atomic
    def handle(self, *args, **opts):
        placed, missing = place_persistent_vessels()
        self.stdout.write(self.style.SUCCESS(f"Tank-map layout: {placed} vessels placed."))
        if missing:
            self.stdout.write(self.style.WARNING(
                "No vessel found for these codes (run seed_reference first, or fix the "
                "code): " + ", ".join(missing)))
