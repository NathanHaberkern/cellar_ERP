"""Load parsed club requests (JSON list) and plan each one. Writes nothing to C7.

    python manage.py clubdesk_load requests.json
    python manage.py clubdesk_load requests.json --no-propose

Each record: mailbox, message_id, received_at (ISO), customer_email, customer_name,
first_name, request_text, kind (skip/pause/cancel/hold_pickup/delay/address/swap/
refund/card/status/other), details {...}, requested_date (YYYY-MM-DD).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from clubdesk.services.propose import load, propose
from integrations.commerce7 import C7Error, Commerce7Client


class Command(BaseCommand):
    help = "Load parsed club request emails and propose changes for approval."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--no-propose", action="store_true")

    def handle(self, *args, **o):
        with open(o["path"], encoding="utf-8") as f:
            records = json.load(f)
        made = load(records)
        self.stdout.write(f"Loaded {len(made)} new request(s) of {len(records)}.")
        if o["no_propose"] or not made:
            return
        client = Commerce7Client.from_settings()
        for req in made:
            try:
                propose(req, client)
            except C7Error as e:
                raise CommandError(f"{req}: {e}")
            self.stdout.write(f"  {req.get_status_display():<32} {req}")
