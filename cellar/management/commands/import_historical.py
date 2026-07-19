"""
Back-load a historical vintage from CSVs.

    python manage.py import_historical historical/2023            # dry run
    python manage.py import_historical historical/2023 --yes
    python manage.py import_historical historical/2023 --yes --overhead-pool 41500

Point it at a DIRECTORY holding the seven numbered files (missing files are simply
skipped, so a vintage with no bulk sales needs no 06_removals.csv). Files are read
and validated in full BEFORE anything is written — see the module docstring in
cellar/services/historical_import.py for why a partial write into an append-only
ledger is the thing to avoid.

--overhead-pool takes one dollar figure for the whole vintage and spreads it across
the imported lots by gallons produced, booked as LotCostAdjustment(basis=allocated)
so it is distinguishable from a per-lot figure you entered by hand.

Re-running is safe: every writer keys on a natural identity (lot code, weigh-tag
number, sku+date) and skips what already exists.
"""
import os

from django.core.management.base import BaseCommand, CommandError

from cellar.services.historical_import import FILE_ORDER, Importer


class Command(BaseCommand):
    help = "Import a historical vintage (2023/2024) from a directory of CSVs."

    def add_arguments(self, parser):
        parser.add_argument("directory", help="folder holding the numbered CSVs")
        parser.add_argument("--yes", action="store_true",
                            help="Actually write. Without this it is a dry run.")
        parser.add_argument("--overhead-pool", type=str, default=None,
                            help="total $ of cellar overhead to spread across the "
                                 "imported lots by volume")

    def handle(self, *args, **opts):
        directory = opts["directory"]
        if not os.path.isdir(directory):
            raise CommandError(f"{directory} is not a directory")

        present = [f for f in FILE_ORDER if os.path.exists(os.path.join(directory, f))]
        if not present:
            raise CommandError(
                f"No import files found in {directory}.\nExpected any of:\n  "
                + "\n  ".join(FILE_ORDER))

        imp = Importer(directory, stdout=self)
        self.stdout.write(self.style.WARNING(f"Reading {directory} …"))
        imp.parse_all()
        ok = imp.report()

        if not ok:
            self.stdout.write(self.style.ERROR(
                f"\n{len(imp.errors)} error(s). Nothing written. Fix the CSVs and re-run."))
            return

        if not opts["yes"]:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — everything parsed and cross-checked, nothing written.\n"
                "Re-run with --yes to commit."))
            return

        stats = imp.write(overhead_pool=opts["overhead_pool"])
        self.stdout.write(self.style.SUCCESS("\nWritten:"))
        for k in sorted(stats):
            self.stdout.write(f"  {stats[k]:>6}  {k}")
        self.stdout.write(self.style.SUCCESS(
            "\nDone. Spot-check a lot page, then run the COGS report before you "
            "trust the dollar figures."))

    # Importer calls self.out.write(msg) with a single argument.
    def write(self, msg=""):
        self.stdout.write(msg)
