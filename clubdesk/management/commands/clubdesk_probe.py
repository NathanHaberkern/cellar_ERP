"""Read-only check of the club routes this app depends on. GET requests only.

    python manage.py clubdesk_probe

Prints which routes answer, the shape of a membership (including onHolds) and of
a club package, and tries the sub-routes Commerce7 admin may use for member
selections. Customer names, emails and addresses are never printed.
"""
import json

from django.core.management.base import BaseCommand

from integrations.commerce7 import C7Error, Commerce7Client

PII = {"firstName", "lastName", "email", "emails", "phone", "phones", "address", "address2", "city",
       "zipCode", "birthDate", "company", "lastFour", "cardHolderName", "content",
       "shippingInstructions", "giftMessage"}

SUBROUTES = {
    "club-package/{pkg}": ["club-membership", "membership", "customization", "item", "product"],
    "club-membership/{mem}": ["club-package", "package", "customization", "on-hold", "shipment"],
}


def shape(o, depth=0):
    if isinstance(o, dict):
        return {k: "<hidden>" if k in PII else shape(v, depth + 1) for k, v in o.items()} \
            if depth < 4 else "{...}"
    if isinstance(o, list):
        return [shape(o[0], depth + 1), f"({len(o)} items)"] if o else []
    return o


class Command(BaseCommand):
    help = "GET-only check of Commerce7 club, membership, package and note routes."

    def _get(self, client, path, params=None):
        try:
            return 200, client.get(path, params)
        except C7Error as e:
            return e.status_code, None

    def handle(self, *args, **o):
        c = Commerce7Client.from_settings()
        ids = {}
        for path, key in (("club", "clubs"), ("club-package", "clubPackages"),
                          ("club-membership", "clubMemberships"), ("tax", "taxes")):
            code, data = self._get(c, path)
            n = len((data or {}).get(key) or [])
            self.stdout.write(f"GET /{path:<16} {code}  {n} rows")
            if data and data.get(key):
                ids[path] = data[key][0]["id"]
        if "club-membership" in ids:
            _, m = self._get(c, f"club-membership/{ids['club-membership']}")
            self.stdout.write("membership shape:\n" + json.dumps(shape(m), indent=1, default=str))
        if "club-package" in ids:
            _, p = self._get(c, f"club-package/{ids['club-package']}")
            self.stdout.write("package shape:\n" + json.dumps(shape(p), indent=1, default=str)[:5000])
        for base, subs in SUBROUTES.items():
            ref = ids.get("club-package" if "{pkg}" in base else "club-membership")
            if not ref:
                continue
            for sub in subs:
                path = base.format(pkg=ref, mem=ref) + "/" + sub
                code, _ = self._get(c, path)
                self.stdout.write(f"GET /{path.split('/')[0]}/<id>/{sub:<16} {code}")
        code, _ = self._get(c, "note", {"customerId": "00000000-0000-0000-0000-000000000000"})
        self.stdout.write(f"GET /note {code}")
