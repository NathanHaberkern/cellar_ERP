"""After a release builds and shipping_pull brings the club orders in, set the
requested ship date on orders whose members asked for a later delivery before
the build.

    python manage.py clubdesk_apply_dates
"""
from django.core.management.base import BaseCommand

from clubdesk.services.propose import apply_pending_dates


class Command(BaseCommand):
    help = "Copy pre-build delay requests onto the built club orders."

    def handle(self, *args, **o):
        changed = apply_pending_dates()
        for order in changed:
            self.stdout.write(f"  #{order.c7_order_number} held until {order.requested_ship_date:%m/%d}")
        self.stdout.write(f"{len(changed)} order(s) updated.")
