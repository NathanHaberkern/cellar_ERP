"""Diff the posted cost ledger against a fresh live computation.

A stored ledger can drift from its sources in a way the derived compliance ledger
cannot. This is the check for that. Run it before closing any period.

    python manage.py cost_reconcile
    python manage.py cost_reconcile --show-all
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Reconcile posted CostEntry totals against computed lot costs."

    def add_arguments(self, parser):
        parser.add_argument("--show-all", action="store_true",
                            help="list every lot, not just the ones that differ")
        parser.add_argument("--tolerance", default="0.05")

    def handle(self, *args, **opts):
        from decimal import Decimal
        from cellar.services import cost_ledger

        rows = cost_ledger.reconcile(tolerance=Decimal(opts["tolerance"]))
        if not rows:
            self.stdout.write("Nothing posted yet — run `manage.py post_costs` first.")
            return

        bad = [r for r in rows if not r["ok"]]
        show = rows if opts["show_all"] else bad

        for r in show:
            style = self.style.SUCCESS if r["ok"] else self.style.ERROR
            self.stdout.write(style(
                f"  {r['lot'].code:<14} posted ${r['posted']:>12} "
                f"(out ${r['transferred_out']:>10})  computed ${r['computed']:>12}  "
                f"diff ${r['diff']:>10}"))

        self.stdout.write("")
        if bad:
            self.stdout.write(self.style.ERROR(
                f"{len(bad)} of {len(rows)} lot(s) DO NOT reconcile."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"All {len(rows)} posted lot(s) reconcile within ${opts['tolerance']}."))

        self.stdout.write(f"Total WIP (posted, capitalized): ${cost_ledger.wip_total()}")
