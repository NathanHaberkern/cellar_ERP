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
    Vessel (tanks only — see below), Container, Rack, Room, Location, BarrelOrder,
    Additive, LabAnalyte, LabAnalyteSynonym,
    BottleFormat, DryGood, Material, ConfigConstant, TaskRule, FruitPrice, users

DELETED (everything the cellar recorded):
    every other model in the app — lots, designations, lineage, weigh tags,
    readings, additions, volumes, tank/rack assignments, aging placements,
    topping, pressing, fortification, bottling, bond + tax rows, lab requests /
    results / values, notes, tasks, task events, and the lot sequence counters.
    ALSO: macro-bin / 1-ton-bin Vessel rows. Those aren't master data — each one
    is created fresh per crush (`code=f"{lot.code}·{label}"`, in
    services/operations.py) and is meaningless once its lot is gone. Real tanks
    (Vessel.Type.TANK) are untouched.

The sequence counters matter: leave them and your first re-crushed lot comes back
as seq 2, because the counter only ever increments. Bin vessels matter for the
same reason in reverse: leave a stale "25VERD·A" behind after a reset, and the
NEXT crush regenerates that exact same lot code + bin label and collides on
Vessel.code's unique constraint.
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

# Vessel is in KEEP (real tanks are master data), but bin-type vessels are
# per-crush and transactional in spirit. Their `type` values, not `Vessel`
# model membership, decide what to keep.
BIN_VESSEL_TYPES = ("macro_bin", "one_ton_bin")


class Command(BaseCommand):
    help = "Delete all transactional data (lots + events); keep reference data."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Actually delete. Without this it is a dry run.")

    def handle(self, *args, **opts):
        commit = opts["yes"]
        models = [m for m in apps.get_app_config("cellar").get_models()
                  if m.__name__ not in KEEP]

        from cellar.models import Vessel
        bin_vessel_count = Vessel.objects.filter(type__in=BIN_VESSEL_TYPES).count()
        tank_vessel_count = Vessel.objects.exclude(type__in=BIN_VESSEL_TYPES).count()

        counts = {m.__name__: m.objects.count() for m in models}
        total = sum(counts.values()) + bin_vessel_count

        self.stdout.write(self.style.WARNING("Transactional rows to delete:"))
        for name in sorted(counts):
            if counts[name]:
                self.stdout.write(f"  {counts[name]:>6}  {name}")
        if bin_vessel_count:
            self.stdout.write(f"  {bin_vessel_count:>6}  Vessel (macro/1-ton bins only)")
        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to delete — already clean."))
            return

        kept = {m.__name__: m.objects.count()
                for m in apps.get_app_config("cellar").get_models()
                if m.__name__ in KEEP and m.__name__ != "Vessel"}
        self.stdout.write(self.style.SUCCESS("\nKeeping (reference data):"))
        for name in sorted(kept):
            if kept[name]:
                self.stdout.write(f"  {kept[name]:>6}  {name}")
        self.stdout.write(f"  {tank_vessel_count:>6}  Vessel (tanks — bins are deleted, see above)")

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
                    # Vessel itself stays in KEEP (real tanks are master data), but
                    # bin-type rows are transactional — delete just those, by type,
                    # rather than clearing the whole table.
                    if bin_vessel_count:
                        placeholders = ", ".join(["%s"] * len(BIN_VESSEL_TYPES))
                        cur.execute(
                            f'DELETE FROM "{Vessel._meta.db_table}" '
                            f'WHERE type IN ({placeholders})',
                            list(BIN_VESSEL_TYPES),
                        )
            # re-validate the graph before we commit: if a KEEP model still points at
            # something we deleted, fail loudly here rather than leaving a broken DB.
            for m in models:
                connection.check_constraints(table_names=[m._meta.db_table])
            connection.check_constraints(table_names=[Vessel._meta.db_table])

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {total} rows (including {bin_vessel_count} bin vessels). "
            "Reference data (and real tanks) intact. "
            "The cellar is empty — re-crush against the curated catalog."))
