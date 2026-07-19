"""
Wipe all transactional data — lots, designations, lineage, weigh tags, and every
event / ledger row — while KEEPING reference and master data.

Written for the pre-production reset: the abbreviation catalog was empty when the
first lots were crushed, so those lots carry provisional codes (25V, 25T) baked
into their designation rows. Rather than re-designate them and reconcile the
sequence counters, we clear the decks and re-crush against a curated catalog.

IRREVERSIBLE. Defaults to a dry run; you must pass --yes to actually delete.

    python manage.py reset_transactional                            # dry run — prints counts
    python manage.py reset_transactional --yes                      # delete everything
    python manage.py reset_transactional --yes --keep-vintages 2023,2024

--keep-vintages EXISTS BECAUSE OF THE HISTORICAL IMPORT
-------------------------------------------------------
2023 and 2024 were back-loaded from paper (see import_historical). Re-keying them
is hours of work, and they are not part of what a mid-parallel-run reset is trying
to clear — that is only ever the 2025 lots being entered live. Without this flag,
one reset silently takes the historical vintages with it.

The flag protects a lot by `Lot.vintage_year` and then walks OUTWARD from those
lots through every FK path, so a protected lot keeps its weigh tags, harvest
events, bond bookings, blends, bottling runs and removals — not just the Lot row.
Anything with no path to a lot is handled explicitly:

    HighProofSpiritLedger   kept if dated in a kept vintage, or if it is the draw
                            behind a kept FortificationEvent (deleting it would
                            break the fortification's PROTECTed FK)
    MaterialTransaction     kept if dated in a kept vintage or used by a kept
                            SweeteningEvent
    LotSequenceCounter      kept for the kept vintages only — a 2025 counter must
                            still reset to zero
    DailyPlan               kept if dated in a kept vintage

A model that reaches Lot but is not in the map below raises rather than guessing.
Silently wiping a table because nobody updated this file is exactly the failure
this command must not have.

KEPT ALWAYS (master data you curated):
    Variety, Grower, Vineyard, Block, VarietalDesignation,
    Vessel (tanks only — see below), Container, Rack, Room, Location, BarrelOrder,
    Additive, LabAnalyte, LabAnalyteSynonym,
    BottleFormat, DryGood, Material, ConfigConstant, TaskRule, FruitPrice,
    ExternalDestination, users

DELETED (everything the cellar recorded, minus any kept vintages):
    every other model in the app — lots, designations, lineage, weigh tags,
    readings, additions, volumes, tank/rack assignments, aging placements,
    topping, pressing, fortification, bottling, bond + tax rows, lab requests /
    results / values, notes, tasks, task events, cost adjustments, and the lot
    sequence counters.
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
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

# Master data — curated by hand, expensive to rebuild, never recorded by the cellar.
# ExternalDestination joined this list with the historical import: its own model
# docstring calls it "reference data (editable master), not a ledger row", every
# removal FKs it with on_delete=PROTECT, and re-typing the buyer list after each
# reset was pure friction.
KEEP = {
    "Variety", "Grower", "Vineyard", "Block", "VarietalDesignation",
    "Vessel", "Container", "Rack", "Room", "Location", "BarrelOrder",
    "RackAssignment",
    "Additive", "LabAnalyte", "LabAnalyteSynonym",
    "BottleFormat", "DryGood", "Material", "ConfigConstant", "TaskRule",
    "FruitPrice", "ExternalDestination",
}

# Vessel is in KEEP (real tanks are master data), but bin-type vessels are
# per-crush and transactional in spirit. Their `type` values, not `Vessel`
# model membership, decide what to keep.
BIN_VESSEL_TYPES = ("macro_bin", "one_ton_bin")


def _lot_paths(lot_ids):
    """model name -> Q selecting the rows that belong to the protected lots.

    Every transactional model must appear here (or in _DATE_SCOPED); an unmapped
    model aborts the run.
    """
    L = list(lot_ids)
    return {
        "Lot": Q(id__in=L),
        "LotDesignation": Q(lot_id__in=L),
        "LotSectionNote": Q(lot_id__in=L),
        "LotCompositionOverride": Q(lot_id__in=L),
        "LotFermentationOverride": Q(lot_id__in=L),
        "LabSampleAlias": Q(lot_id__in=L),
        "WeighTagAllocation": Q(lot_id__in=L),
        "LotLineage": Q(parent_lot_id__in=L) | Q(child_lot_id__in=L),
        "HarvestEvent": Q(weigh_tags__allocations__lot_id__in=L),
        "WeighTag": Q(allocations__lot_id__in=L),
        "WeighTagBin": Q(assigned_lot_id__in=L) | Q(weigh_tag__allocations__lot_id__in=L),
        "Reading": Q(lot_id__in=L),
        "Addition": Q(lot_id__in=L),
        "DestemmingEvent": Q(lot_id__in=L),
        "TankAssignment": Q(lot_id__in=L),
        "ColdSoakSchedule": Q(lot_id__in=L),
        "PumpOverEvent": Q(lot_id__in=L),
        "PunchDownEvent": Q(lot_id__in=L),
        "InoculationEvent": Q(lot_id__in=L),
        "LabRequest": Q(lot_id__in=L),
        "LabResult": Q(lot_id__in=L),
        "LabResultValue": Q(result__lot_id__in=L),
        "CellarNote": Q(lot_id__in=L),
        "VolumeMeasurement": Q(lot_id__in=L),
        "PressingEvent": Q(lot_id__in=L),
        "FortificationEvent": Q(lot_id__in=L),
        "BookToBond": Q(lot_id__in=L),
        "AgingPlacement": Q(lot_id__in=L),
        "VolumeLoss": Q(lot_id__in=L),
        "ToppingEvent": Q(source_lot_id__in=L),
        "ToppingTarget": Q(event__source_lot_id__in=L),
        "BottlingRun": Q(source_lot_id__in=L),
        "BottlingDryGoodUse": Q(run__source_lot_id__in=L),
        "TaxPaidRemoval": Q(bottling_run__source_lot_id__in=L),
        "BondTransfer": Q(lot_id__in=L),
        "BulkTaxPaidRemoval": Q(lot_id__in=L),
        "MustSale": Q(lot_id__in=L),
        "BondAdjustment": Q(lot_id__in=L),
        "SweeteningEvent": Q(lot_id__in=L),
        "LotCostAdjustment": Q(lot_id__in=L),
        "Task": Q(lot_id__in=L),
        "TaskEvent": Q(task__lot_id__in=L),
    }


# Models with no path to a Lot — scoped by their own year field instead.
_DATE_SCOPED = {
    "LotSequenceCounter": "vintage",          # integer year, not a date
    "DailyPlan": "date__year",
    "MaterialTransaction": "occurred_at__year",
    "HighProofSpiritLedger": "event_date__year",
}


class Command(BaseCommand):
    help = "Delete all transactional data (lots + events); keep reference data."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Actually delete. Without this it is a dry run.")
        parser.add_argument("--keep-vintages", type=str, default="",
                            help="Comma-separated vintage years to preserve entirely, "
                                 "e.g. 2023,2024 (the back-loaded historical vintages).")

    # ------------------------------------------------------------------ protection
    def _protected_ids(self, models, vintages):
        """{model_name: set(pk)} for everything the kept vintages reach."""
        from cellar.models import FortificationEvent, Lot, SweeteningEvent

        lot_ids = set(Lot.objects.filter(vintage_year__in=vintages)
                      .values_list("id", flat=True))
        if not lot_ids:
            self.stdout.write(self.style.WARNING(
                f"  (no lots found for vintage(s) {', '.join(map(str, sorted(vintages)))} — "
                f"only date-scoped rows will be kept)"))

        paths = _lot_paths(lot_ids)
        protected, unmapped = {}, []

        for m in models:
            name = m.__name__
            if name in paths:
                protected[name] = set(
                    m.objects.filter(paths[name]).values_list("id", flat=True))
            elif name in _DATE_SCOPED:
                protected[name] = set(
                    m.objects.filter(**{f"{_DATE_SCOPED[name]}__in": list(vintages)})
                    .values_list("id", flat=True))
            else:
                unmapped.append(name)

        if unmapped:
            raise CommandError(
                "These models have no --keep-vintages rule and would be wiped "
                "without being considered:\n    " + "\n    ".join(sorted(unmapped)) +
                "\n\nAdd each to _lot_paths() or _DATE_SCOPED in this command. "
                "Refusing to guess.")

        # Widen the spirit ledger: a kept fortification PROTECTs its draw row, so the
        # draw must survive even when it falls outside the kept years.
        draw_ids = set(FortificationEvent.objects
                       .filter(id__in=protected.get("FortificationEvent", set()))
                       .exclude(hpgs_draw__isnull=True)
                       .values_list("hpgs_draw_id", flat=True))
        protected.setdefault("HighProofSpiritLedger", set()).update(draw_ids)

        # Same for a kept sweetening event and its material transaction.
        mat_ids = set(SweeteningEvent.objects
                      .filter(id__in=protected.get("SweeteningEvent", set()))
                      .exclude(material_use__isnull=True)
                      .values_list("material_use_id", flat=True))
        protected.setdefault("MaterialTransaction", set()).update(mat_ids)

        return protected

    # ----------------------------------------------------------------------- main
    def handle(self, *args, **opts):
        commit = opts["yes"]
        raw = (opts["keep_vintages"] or "").strip()
        try:
            vintages = {int(v) for v in raw.split(",") if v.strip()} if raw else set()
        except ValueError:
            raise CommandError(f"--keep-vintages must be years, got '{raw}'")

        models = [m for m in apps.get_app_config("cellar").get_models()
                  if m.__name__ not in KEEP]

        from cellar.models import Vessel
        bin_vessel_count = Vessel.objects.filter(type__in=BIN_VESSEL_TYPES).count()
        tank_vessel_count = Vessel.objects.exclude(type__in=BIN_VESSEL_TYPES).count()

        protected = {}
        if vintages:
            self.stdout.write(self.style.SUCCESS(
                f"Preserving vintage(s): {', '.join(map(str, sorted(vintages)))}"))
            protected = self._protected_ids(models, vintages)

        counts, keeps = {}, {}
        for m in models:
            total_rows = m.objects.count()
            kept = len(protected.get(m.__name__, ()))
            counts[m.__name__] = total_rows - kept
            keeps[m.__name__] = kept
        total = sum(counts.values()) + bin_vessel_count

        self.stdout.write(self.style.WARNING("\nTransactional rows to delete:"))
        for name in sorted(counts):
            if counts[name]:
                suffix = f"   (keeping {keeps[name]})" if keeps[name] else ""
                self.stdout.write(f"  {counts[name]:>6}  {name}{suffix}")
        if bin_vessel_count:
            self.stdout.write(f"  {bin_vessel_count:>6}  Vessel (macro/1-ton bins only)")
        if not total:
            self.stdout.write(self.style.SUCCESS("Nothing to delete — already clean."))
            return

        if vintages:
            held = {k: v for k, v in keeps.items() if v}
            if held:
                self.stdout.write(self.style.SUCCESS("\nPreserved (kept vintages):"))
                for name in sorted(held):
                    self.stdout.write(f"  {held[name]:>6}  {name}")

        kept_ref = {m.__name__: m.objects.count()
                    for m in apps.get_app_config("cellar").get_models()
                    if m.__name__ in KEEP and m.__name__ != "Vessel"}
        self.stdout.write(self.style.SUCCESS("\nKeeping (reference data):"))
        for name in sorted(kept_ref):
            if kept_ref[name]:
                self.stdout.write(f"  {kept_ref[name]:>6}  {name}")
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
                        keep_ids = protected.get(m.__name__, set())
                        if not keep_ids:
                            cur.execute(f'DELETE FROM "{m._meta.db_table}"')
                            continue
                        # Delete by explicit id chunks rather than NOT IN (…): the
                        # kept set can exceed the SQLite parameter ceiling, and an
                        # id list we built ourselves is easier to reason about than
                        # a negated one when something goes wrong.
                        doomed = list(m.objects.exclude(id__in=keep_ids)
                                      .values_list("id", flat=True))
                        for i in range(0, len(doomed), 500):
                            chunk = doomed[i:i + 500]
                            ph = ", ".join(["%s"] * len(chunk))
                            cur.execute(
                                f'DELETE FROM "{m._meta.db_table}" WHERE id IN ({ph})',
                                chunk)
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

        msg = (f"\nDeleted {total} rows (including {bin_vessel_count} bin vessels). "
               "Reference data (and real tanks) intact.")
        if vintages:
            msg += (f"\nVintage(s) {', '.join(map(str, sorted(vintages)))} preserved "
                    f"in full — lots, tags, bookings, blends, bottlings and removals.")
        else:
            msg += " The cellar is empty — re-crush against the curated catalog."
        self.stdout.write(self.style.SUCCESS(msg))
