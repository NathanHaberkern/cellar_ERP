"""Pull open ship orders from Commerce7 and check new addresses with FedEx.

    python manage.py shipping_pull
    python manage.py shipping_pull --since 2026-09-01 --no-validate

Same as the "Pull orders" button on the Shipping page.
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from integrations.commerce7 import C7Error
from shipping.carriers import CarrierError
from shipping.services.intake import sync_orders, validate_new_orders


class Command(BaseCommand):
    help = "Sync unfulfilled ship orders from C7, then FedEx-validate new addresses."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=date.fromisoformat, help="paid on/after YYYY-MM-DD")
        parser.add_argument("--no-validate", action="store_true")

    def handle(self, *args, **o):
        try:
            result = sync_orders(since=o["since"])
        except C7Error as e:
            raise CommandError(str(e))
        self.stdout.write(f"C7: {result.summary()}")
        for err in result.errors:
            self.stdout.write(self.style.ERROR(f"  {err}"))
        if o["no_validate"]:
            return
        try:
            counts = validate_new_orders()
        except CarrierError as e:
            raise CommandError(f"FedEx: {e}")
        if counts["checked"]:
            self.stdout.write(f"FedEx: {counts['ready']} ready, {counts['review']} need address review")
        else:
            self.stdout.write(f"{counts['ready']} ready (address check off: FedEx test keys)")
