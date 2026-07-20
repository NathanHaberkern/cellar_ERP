"""Expense losses flagged abnormal on VolumeLoss.

Normal loss needs no command — it capitalizes itself, because cost_basis_volume()
divides an unchanged dollar total by a smaller balance and cost/gal simply rises.
Only abnormal loss has to be taken back out of inventory.

    python manage.py post_abnormal_losses
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Post CostEntry credit/expense pairs for abnormal volume losses."

    def handle(self, *args, **opts):
        from cellar.services import overhead
        made = overhead.post_abnormal_losses()
        if not made:
            self.stdout.write("No unposted abnormal losses.")
            return
        for r in made:
            where = r.lot.code if r.lot_id else "period expense"
            self.stdout.write(f"  {r.occurred_at}  {where:<16} ${r.amount}")
        self.stdout.write(self.style.SUCCESS(
            f"\nPosted {len(made)} row(s) — {len(made) // 2} loss(es) taken out of inventory."))
