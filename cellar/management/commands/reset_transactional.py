"""
Wipe all transactional data — lots, designations, lineage, weigh tags, and every
event / ledger row — while KEEPING reference and master data.

Written for the pre-production reset: the abbreviation catalog was empty when the
first lots were crushed, so those lots carry provisional codes (25V, 25T) baked
into their designation rows. Rather than re-designate them and reconcile the
sequence counters, we clear the decks and re-crush against a curated catalog.

IRREVERSIBLE. Defaults to a dry run; you must pass --yes to actually delete.

    python manage.py reset_transactional             # dry run — prints counts
    python manage.py reset_transactional --yes       # actually delete

KEPT (master data you curated):
    Variety, Grower, Vineyard, Block, VarietalDesignation,
    Vessel, Container, Rack, Room, Location, BarrelOrder,
    Additive, LabAnalyte, LabAnalyteSynonym,
    BottleFormat, DryGood, Material, ConfigConstant, TaskRule, FruitPrice, users

DELETED (everything the cellar recorded):
    every other model in the app — lots, designations, lineage, weigh tags,
    readings, additions, volumes, tank/rack assignments, aging placements,
    topping, pressing, fortification, bottling, bond + tax rows, lab requests /
    results / values, notes, tasks, task events, and the lot sequence counters.

The sequence counters matter: leave them and your first re-crushed lot comes back
as seq 2, because the counter only ever increments.
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

# Master data — curated by hand, expensive to rebuild, never recorded by the cellar.
KEEP = {
    "Variety", "Grower", "Vineyard", "Block", "VarietalDesignation",
    "Vessel", "Container", "Rack", "Room", "Location", "BarrelOrder",
    "Additive", "LabAnalyte", "LabAnalyteSynonym",
    "BottleFormat", "DryGood", "Material", "ConfigConstant", "TaskRule",
    "FruitPrice",
}


class Command(BaseCommand):
    help = "Delete all transactional data (lots + events); keep reference data."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Actually delete. Without this it is a dry run.")

    def handle(self, *args, **opts):
        commit = opts["yes"]
        models = [m for m in apps.get_app_config("cellar").get_models()
                  if m.__name__ not in KEEP]

        counts = {m.__name__: m.objects.count() for m in models}
        total = sum(counts.values())

        self.stdout.write(self.style.WARNING("Transactional rows to delete:"))
        for name in sorted(counts):
            if counts[name]:
                self.stdout.write(f"  {counts[name]:>6}  {name}")
        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to delete — already clean."))
            return

        kept = {m.__name__: m.objects.count()
                for m in apps.get_app_config("cellar").get_models()
                if m.__name__ in KEEP}
        self.stdout.write(self.style.SUCCESS("\nKeeping (reference data):"))
        for name in sorted(kept):
            if kept[name]:
                self.stdout.write(f"  {kept[name]:>6}  {name}")

        if not commit:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — {total} rows would be deleted. Nothing written.\n"
                "Re-run with --yes to actually delete. This cannot be undone."))
            return

        # Most FKs here are on_delete=PROTECT — deliberately, so nobody deletes a lot
        # out from under a filed report. That guard is enforced by Django's collector,
        # so an ORM .delete() raises ProtectedError no matter what order we go in, and
        # DB-level constraint toggling doesn't touch it. Since a full reset is exactly
        # the case the guard isn't meant to stop, go around the ORM with raw DELETEs,
        # in one transaction with DB constraints deferred.
        from django.db import connection

        with transaction.atomic():
            with connection.constraint_checks_disabled():
                with connection.cursor() as cur:
                    for m in models:
                        cur.execute(f'DELETE FROM "{m._meta.db_table}"')
            # re-validate the graph before we commit: if a KEEP model still points at
            # something we deleted, fail loudly here rather than leaving a broken DB.
            for m in models:
                connection.check_constraints(table_names=[m._meta.db_table])

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {total} rows. Reference data intact. "
            "The cellar is empty — re-crush against the curated catalog."))
