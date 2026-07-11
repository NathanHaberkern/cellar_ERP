"""
Seed the CURATED varietal abbreviation catalog.

This is the table `resolve_abbreviation()` consults to build a lot code. With it
empty, every code autofires provisional from the name stem — which is how 2025
Verdelho came out as "25V". Curated entries here make the code official
(is_curated=True → is_provisional=False on the designation).

Resolution precedence is block > vineyard > variety default, so a vineyard override
is how "Zinfandel from Mohr-Fry" becomes MFZ while Zinfandel elsewhere stays ZINF.

Port and rosé are DERIVED, not listed: resolve_abbreviation() takes the table code
and appends PORT / ROSE (Tempranillo -> TEMPPORT). Only add an explicit row below
if a program needs a code that ISN'T table+suffix.

Only creates varieties/vineyards named here if they already exist — it never
invents master data. Idempotent; re-run freely.

    python manage.py seed_designations            # apply
    python manage.py seed_designations --dry-run  # show what it would do
"""
from django.core.management.base import BaseCommand

from cellar.models import Variety, Vineyard, VarietalDesignation
from cellar.models.base import Program

# ---------------------------------------------------------------------------
# EDIT ME — the house catalog. (variety name, abbreviation)
# Table-program defaults. Port/rosé derive as TABLE + "PORT"/"ROSE".
# ---------------------------------------------------------------------------
VARIETY_DEFAULTS = [
    ("Verdelho",         "VERD"),
    ("Trousseau",        "TROU"),
    ("Tempranillo",      "TEMP"),
    ("Touriga Nacional", "TN"),
    ("Tinta Cão",        "TC"),
    ("Souzão",           "SOUZ"),
    ("Zinfandel",        "ZINF"),
    ("Alvarelhão",       "ALVA"),
    ("Bastardo",         "BAST"),
]

# Vineyard-level overrides — (variety, vineyard name contains, abbreviation)
VINEYARD_OVERRIDES = [
    ("Zinfandel", "Mohr-Fry", "MFZ"),
]

# Explicit program codes, ONLY where table+suffix is wrong.
# (variety, program, abbreviation)
PROGRAM_OVERRIDES = []


class Command(BaseCommand):
    help = "Seed the curated varietal abbreviation catalog."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change; write nothing.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        made = skipped = 0

        def upsert(variety, program, abbr, vineyard=None, label=""):
            nonlocal made, skipped
            existing = VarietalDesignation.objects.filter(
                variety=variety, program=program, block=None, vineyard=vineyard).first()
            if existing and existing.abbreviation == abbr and existing.is_curated:
                skipped += 1
                return
            verb = "update" if existing else "create"
            self.stdout.write(f"  {verb}: {variety.name} · {program}{label} → {abbr}")
            if dry:
                made += 1
                return
            VarietalDesignation.objects.update_or_create(
                variety=variety, program=program, block=None, vineyard=vineyard,
                defaults={"abbreviation": abbr, "is_curated": True})
            made += 1

        for name, abbr in VARIETY_DEFAULTS:
            v = Variety.objects.filter(name__iexact=name).first()
            if v is None:
                self.stdout.write(self.style.WARNING(
                    f"  skip: no Variety named {name!r} — create it first"))
                continue
            upsert(v, Program.TABLE, abbr)

        for vname, vy_match, abbr in VINEYARD_OVERRIDES:
            v = Variety.objects.filter(name__iexact=vname).first()
            vy = Vineyard.objects.filter(name__icontains=vy_match).first()
            if v is None or vy is None:
                self.stdout.write(self.style.WARNING(
                    f"  skip: {vname} @ {vy_match} — variety or vineyard not found"))
                continue
            upsert(v, Program.TABLE, abbr, vineyard=vy, label=f" @ {vy.name}")

        for vname, program, abbr in PROGRAM_OVERRIDES:
            v = Variety.objects.filter(name__iexact=vname).first()
            if v is None:
                continue
            upsert(v, program, abbr)

        note = " (dry run — nothing written)" if dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"Designations: {made} written, {skipped} already correct.{note}"))
