"""Close an accounting month so its posted costs can't be restated.

Refuses to close while any lot fails reconciliation, unless --force.

    python manage.py close_cost_period 2025 10
    python manage.py close_cost_period 2025 10 --force
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Close a CostPeriod."

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("month", type=int)
        parser.add_argument("--force", action="store_true",
                            help="close even though reconciliation is out")

    def handle(self, *args, **opts):
        from cellar.models import CostPeriod
        from cellar.services import cost_ledger

        period = CostPeriod.objects.filter(year=opts["year"], month=opts["month"]).first()
        if period is None:
            self.stderr.write(self.style.ERROR(
                f"No period {opts['year']}-{opts['month']:02d}. Post some costs into it first."))
            return
        try:
            cost_ledger.close_period(period, force=opts["force"])
        except ValueError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        s = cost_ledger.period_summary(period)
        self.stdout.write(self.style.SUCCESS(f"Closed {period.label}."))
        for line in s["lines"]:
            tag = "expense" if line["is_expense"] else "capitalized"
            self.stdout.write(f"  {line['label']:<28} ${line['total']:>12}  ({tag})")
        self.stdout.write(f"\n  Capitalized to inventory: ${s['capitalized']}")
        self.stdout.write(f"  Period expense:           ${s['expense']}")
        if s["deferred"]:
            self.stdout.write(self.style.WARNING(
                f"  {s['deferred']} entr(ies) in this period were deferred from an earlier month."))
