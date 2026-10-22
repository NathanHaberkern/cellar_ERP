"""Pull a sample of real C7 orders, scrubbed of customer details, to settle field names.

    python manage.py c7_probe                 # last 60 days, Ship, Not Fulfilled
    python manage.py c7_probe --days 120 --limit 50
    python manage.py c7_probe --club          # also sample /club-membership
    python manage.py c7_probe --param channel=Club --param complianceStatus=Compliant

Why this exists: the Tranche 2 order sync must read the VinoCheck compliance
status and club membership from C7, and the docs don't pin down how those show
up on our tenant. Rather than guess (or paste keys into a chat), this pulls a
real sample and writes it to label_output/c7_probe.json (gitignored).

What leaves the machine: nothing -- the file stays local. Inside it, names,
street, city, zip, email, phone, birth dates, company, notes, card/token data
and anything that looks like an email are replaced with "~". State and country
codes are kept (carrier routing needs them). Product titles, SKUs, quantities,
statuses, tags, club and shipping fields are kept -- those are what we need.

The file has two parts:
    census  every field path seen, how often, and up to 12 distinct values
            (strings only, already scrubbed) -- the quick read.
    orders  the scrubbed orders themselves.
"""
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.commerce7 import C7Error, Commerce7Client

# Leaf keys whose values are personal. Matched case-insensitively on the key alone.
PII_KEYS = re.compile(
    r"^(first_?name|last_?name|name|full_?name|birth_?date|dob|e?mail(address)?|emails|"
    r"phone(number)?|phones|mobile|address|address1|address2|street.*|city|zip(code)?|"
    r"postal_?code|company|note|notes|.*instructions|gift_?message|ip_?address|"
    r".*card.*|.*token.*|.*password.*|cvv|avatar.*|.*url)$",
    re.I,
)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
REDACTED = "~"
CENSUS_VALUES = 12


def scrub(obj, key=None):
    if isinstance(obj, dict):
        return {k: scrub(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, key) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        # Numbers stay (quantities, prices, weights) unless the key itself is personal.
        if key and PII_KEYS.match(key) and not isinstance(obj, bool) and obj is not None:
            return REDACTED
        return obj
    s = str(obj)
    if key and PII_KEYS.match(key):
        return REDACTED if s else s
    if EMAIL.search(s):
        return REDACTED
    return s


def census(records):
    counts = defaultdict(int)
    values = defaultdict(set)

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(o, list):
            for v in o:
                walk(v, f"{path}[]")
        else:
            counts[path] += 1
            if len(values[path]) < CENSUS_VALUES and (o is None or isinstance(o, (str, bool))):
                values[path].add(json.dumps(o))

    for r in records:
        walk(r, "")
    return {p: {"count": counts[p], "values": sorted(values[p])} for p in sorted(counts)}


class Command(BaseCommand):
    help = "Write a scrubbed sample of C7 orders (and optionally club memberships) to label_output/."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=60, help="orderPaidDate lookback (default 60)")
        parser.add_argument("--limit", type=int, default=40, help="max orders (default 40)")
        parser.add_argument("--all-statuses", action="store_true",
                            help="drop the Ship / Not Fulfilled filters")
        parser.add_argument("--param", action="append", default=[],
                            help="extra C7 filter key=value; repeatable")
        parser.add_argument("--club", action="store_true", help="also sample /club-membership")
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "label_output" / "c7_probe.json"))

    def handle(self, *args, **o):
        try:
            client = Commerce7Client.from_settings()
        except C7Error as e:
            raise CommandError(str(e))

        params = {"orderPaidDate": f"gte:{(date.today() - timedelta(days=o['days'])).isoformat()}"}
        if not o["all_statuses"]:
            params.update({"orderDeliveryMethod": "Ship", "fulfillmentStatus": "Not Fulfilled"})
        for kv in o["param"]:
            if "=" not in kv:
                raise CommandError(f"--param needs key=value, got {kv!r}")
            k, v = kv.split("=", 1)
            params[k.strip()] = v.strip()

        self.stdout.write(f"GET /order {params}")
        try:
            orders = [scrub(r) for r in client.iter_pages("order", "orders", params, o["limit"])]
            out = {"params": params, "order_count": len(orders),
                   "census": census(orders), "orders": orders}
        except C7Error as e:
            raise CommandError(f"{e}\n{json.dumps(e.body)[:500] if e.body else ''}")
        if o["club"]:
            # Club access is a separate app permission in the C7 Dev Center. A
            # missing permission ("Unauthorized User for Route") must not throw
            # away the orders we already have -- record it and keep going.
            try:
                clubs = [scrub(r) for r in client.iter_pages("club-membership", "clubMemberships",
                                                             None, 15)]
                out["club_membership_census"] = census(clubs)
                out["club_memberships"] = clubs
            except C7Error as e:
                out["club_membership_error"] = f"{e} {e.body}"
                self.stdout.write(self.style.WARNING(
                    f"Club memberships skipped: {e}. Grant the app read access to Club "
                    "Memberships in the C7 Dev Center to include them."))

        path = Path(o["out"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"{len(orders)} orders -> {path}"))
        for p, info in out["census"].items():
            if re.search(r"compliance|club|fulfil|status|channel|deliverymethod|tag", p, re.I) \
                    and "[]" not in p.split(".")[0]:
                self.stdout.write(f"  {p}: {', '.join(info['values'][:6])}")
