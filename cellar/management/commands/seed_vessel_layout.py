"""
Seed / update tank-map placement for the persistent vessels.

Idempotent and PLACE-ONLY: it matches each vessel by its existing `code`
(exact first, then case-insensitively) and writes room + (map_row, map_col).
It never creates vessels — seed_reference owns vessel creation — so it can't
spawn phantom duplicates. Any code it can't find is reported, not invented.

Layout reproduces the three room drawings:

  OLD TANK ROOM (4-col U)          NEW TANK ROOM (2 col)     NEW BARREL ROOM
    SS-1  T-102 T-101 T-103          SS-14        SS-6         Titan  SS-Tote 1  SS-Tote 2
    SS-2              SS-5           SS-13        SS-7        (bins render dynamically,
    SS-3              SS-4           SS-12        SS-8         hidden when empty)
                                     SS-11        SS-9
                                     SS-10

    python manage.py seed_vessel_layout
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from cellar.models.reference import Vessel

R = Vessel.Room

# code -> (room, map_row, map_col).  Codes match seed_reference exactly.
LAYOUT = {
    # --- Old Tank Room (4-col U) ---
    "SS-1":  (R.OLD_TANK, 0, 0),
    "T-102": (R.OLD_TANK, 0, 1),
    "T-101": (R.OLD_TANK, 0, 2),
    "T-103": (R.OLD_TANK, 0, 3),
    "SS-2":  (R.OLD_TANK, 1, 0),
    "SS-5":  (R.OLD_TANK, 1, 3),
    "SS-3":  (R.OLD_TANK, 2, 0),
    "SS-4":  (R.OLD_TANK, 2, 3),
    # --- New Tank Room (left col 0 top→bottom, right col 1 top→bottom) ---
    "SS-14": (R.NEW_TANK, 0, 0),
    "SS-13": (R.NEW_TANK, 1, 0),
    "SS-12": (R.NEW_TANK, 2, 0),
    "SS-11": (R.NEW_TANK, 3, 0),
    "SS-10": (R.NEW_TANK, 4, 0),
    "SS-6":  (R.NEW_TANK, 0, 1),
    "SS-7":  (R.NEW_TANK, 1, 1),
    "SS-8":  (R.NEW_TANK, 2, 1),
    "SS-9":  (R.NEW_TANK, 3, 1),
    # --- New Barrel Room (persistent squares; bins are dynamic) ---
    "Titan":     (R.NEW_BARREL, 0, 0),
    "SS-Tote 1": (R.NEW_BARREL, 0, 1),
    "SS-Tote 2": (R.NEW_BARREL, 0, 2),
}


class Command(BaseCommand):
    help = "Place the persistent vessels on the dashboard tank map (idempotent, place-only)."

    @transaction.atomic
    def handle(self, *args, **opts):
        placed, missing = 0, []
        for code, (room, row, col) in LAYOUT.items():
            v = (Vessel.objects.filter(code=code).first()
                 or Vessel.objects.filter(code__iexact=code).first())
            if v is None:
                missing.append(code)
                continue
            v.room, v.map_row, v.map_col = room, row, col
            v.save(update_fields=["room", "map_row", "map_col"])
            placed += 1
        self.stdout.write(self.style.SUCCESS(
            f"Tank-map layout: {placed} vessels placed."))
        if missing:
            self.stdout.write(self.style.WARNING(
                "No vessel found for these codes (run seed_reference first, or fix "
                "the code): " + ", ".join(missing)))
