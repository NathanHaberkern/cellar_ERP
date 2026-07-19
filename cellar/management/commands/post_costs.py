"""Post derived costs into the CostEntry ledger.

Idempotent — every posting is keyed on (source_kind, source_id, category) under a
unique constraint, so running this nightly, twice, or after a crash lands in the
same place. Safe to schedule.

    python manage.py post_costs --dry-run
    python manage.py post_costs
    python manage.py post_costs --lot 25ZIN1
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Post derived lot costs into the CostEntry ledger (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--lot", help="single lot code")

    def handle(self, *args, **opts):
        from cellar.models import Lot
        from cellar.services import cost_ledger
        from cellar.services.historical_import import find_lot

        lots = None
        if opts.get("lot"):
            lot = find_lot(opts["lot"])
            if lot is None:
                self.stderr.write(self.style.ERROR(f"No lot {opts['lot']}."))
                return
            lots = [lot]

        if opts["dry_run"]:
            sid = transaction.savepoint()
            result = cost_ledger.post_all(lots=lots)
            transaction.savepoint_rollback(sid)
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — would post {result['entries']} entries across "
                f"{result['lots']} lot(s); {result['deferred']} into a later period."))
            return

        result = cost_ledger.post_all(lots=lots)
        self.stdout.write(self.style.SUCCESS(
            f"Posted {result['entries']} entries across {result['lots']} lot(s)."))
        if result["deferred"]:
            self.stdout.write(self.style.WARNING(
                f"{result['deferred']} entr(ies) landed in a later period because their own "
                f"month was closed. Each carries a note saying where it belonged."))
