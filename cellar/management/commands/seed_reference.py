"""
Seed the reference masters this rework depends on: the vessel inventory
(persistent tanks/totes) and the additive catalog with dosing metadata.

Idempotent — safe to re-run; uses update_or_create keyed on the natural key.
Bins are NOT seeded here: they have no durable IDs and are created per-lot
(labelled A/B/C) by the destem operation.

    python manage.py seed_reference
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from cellar.models import Vessel, Additive


# code, capacity_gal, max_fruit_tons (None=N/A), refrigerated, pressure_sensor
TANKS = [
    ("SS-1", 1000, None, True, False),
    ("SS-2", 2000, "8.5", True, True),
    ("SS-3", 2000, "8.5", True, True),
    ("SS-4", 2000, "8.5", True, False),
    ("SS-5", 2000, "8.5", True, False),
    ("SS-6", 3000, "8.5", True, True),
    ("SS-7", 3000, "8.5", True, True),
    ("SS-8", 2000, "8.5", True, True),
    ("SS-9", 2000, "8.5", True, True),
    ("SS-10", 2000, "8.5", True, True),
    ("SS-11", 2000, "8.5", True, True),
    ("SS-12", 1500, "6.5", True, False),
    ("SS-13", 1500, "6.5", True, False),
    ("SS-14", 1500, "6.5", True, False),
    ("SS-Tote 1", 450, "2", True, False),
    ("SS-Tote 2", 450, "2", True, False),
    ("Titan", 550, "2", True, False),
    ("T-101", 1500, None, False, False),
    ("T-102", 1500, None, False, False),
    ("T-103", 1000, None, False, False),
]

C = Additive.Category
M = Additive.DoseMode

# name, category, unit, dose_mode, default_rate, rate_unit, default_ppm, so2_fraction
ADDITIVES = [
    ("KMBS",                 C.SO2,      "g",  M.PPM_TARGET, None,   "",           "40", "0.5764"),
    ("D21",                  C.YEAST,    "lb", M.PER_VOLUME, "2",    "lb/1000gal", None, None),
    ("GRE",                  C.YEAST,    "lb", M.PER_VOLUME, "2",    "lb/1000gal", None, None),
    ("Go-Ferm Sterol Flash", C.NUTRIENT, "g",  M.PER_VOLUME, "30",   "g/hL",       None, None),
    ("Fermaid O",            C.NUTRIENT, "g",  M.PER_VOLUME, "20",   "g/hL",       None, None),
    ("Booster Blanc",        C.NUTRIENT, "g",  M.PER_VOLUME, "2.5",  "lb/1000gal", None, None),
    ("Booster Rouge",        C.NUTRIENT, "g",  M.PER_VOLUME, "2.5",  "lb/1000gal", None, None),
    ("Noblesse",             C.NUTRIENT, "g",  M.PER_VOLUME, "1.7",  "lb/1000gal", None, None),
    ("Tartaric",             C.ACID,     "lb", M.PER_VOLUME, "3.5",  "lb/1000gal", None, None),
    ("Citric",               C.ACID,     "lb", M.PER_VOLUME, "3.5",  "lb/1000gal", None, None),
    ("Color Pro",            C.ENZYME,   "mL", M.PER_TON,    "75",   "mL/ton",     None, None),
    ("Opti-Red",             C.ENZYME,   "g",  M.PER_VOLUME, "2.5",  "lb/1000gal", None, None),
    ("Bactiless",            C.ENZYME,   "g",  M.PER_VOLUME, "2",    "lb/1000gal", None, None),
    ("FT Rouge",             C.TANNIN,   "g",  M.PER_VOLUME, "2.5",  "lb/1000gal", None, None),
    ("Reduless",             C.FINING,   "g",  M.PER_VOLUME, "2",    "g/hL",       None, None),
    ("Gelarom",              C.FINING,   "mL", M.PER_VOLUME, "1.88", "L/1000gal",  None, None),
    ("Celstab",              C.FINING,   "mL", M.PER_VOLUME, "75",   "mL/hL",      None, None),
    ("Fermobent",            C.FINING,   "g",  M.PER_VOLUME, "100",  "g/hL",       None, None),
    ("Copper Sulfate",       C.FINING,   "mL", M.BENCH,      None,   "",           None, None),
]


class Command(BaseCommand):
    help = "Seed vessels and additives (idempotent)."

    def handle(self, *args, **opts):
        def dec(x):
            return Decimal(x) if x is not None else None

        v = 0
        for code, cap, tons, refrig, sensor in TANKS:
            Vessel.objects.update_or_create(
                code=code,
                defaults=dict(
                    type=Vessel.Type.TANK,
                    capacity_gal=Decimal(cap),
                    max_fruit_tons=dec(tons),
                    refrigerated=refrig,
                    temp_controlled=refrig,
                    volume_method=(Vessel.VolumeMethod.PRESSURE_SENSOR if sensor
                                   else Vessel.VolumeMethod.NONE),
                ),
            )
            v += 1

        a = 0
        for name, cat, unit, mode, rate, runit, ppm, so2 in ADDITIVES:
            Additive.objects.update_or_create(
                name=name,
                defaults=dict(
                    category=cat, unit=unit, dose_mode=mode,
                    default_rate=dec(rate), rate_unit=runit,
                    default_target_ppm=dec(ppm), so2_fraction=dec(so2),
                ),
            )
            a += 1

        from cellar.web.tankmap import place_persistent_vessels
        placed, missing = place_persistent_vessels()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {v} vessels, {a} additives; {placed} placed on the tank map."))
        if missing:
            self.stdout.write(self.style.WARNING(
                "Not placed (code mismatch): " + ", ".join(missing)))
