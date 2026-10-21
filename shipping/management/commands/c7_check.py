"""Diagnose Commerce7 credentials without printing the secret.

    python manage.py c7_check

C7_APP_ID and C7_TENANT_ID are identifiers, not secrets, so they are shown in
full -- most 401s are the wrong thing pasted into one of them (e.g. the app's
display name instead of its ID, or a numeric account ID instead of the tenant
slug). C7_APP_SECRET is shown only as a length plus any problem characters.
Same .env checks as fedex_check: quotes, stray spaces, duplicates, and a shell
variable silently overriding .env. Then makes one GET /order?limit=1 call.
"""
import os
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from integrations.commerce7 import C7Error, Commerce7Client

from .fedex_check import _env_file_lines

KEYS = ("C7_APP_ID", "C7_APP_SECRET", "C7_TENANT_ID")


def _problem_chars(val):
    """Characters that are never legitimately in a C7 key: whitespace, quotes,
    zero-width / non-breaking spaces and other non-ASCII picked up by copy/paste."""
    out = []
    for i, ch in enumerate(val, 1):
        if ch.isascii() and ch.isprintable() and not ch.isspace() and ch not in "'\"`":
            continue
        out.append(f"pos {i}: {unicodedata.name(ch, f'U+{ord(ch):04X}')}")
    return out


class Command(BaseCommand):
    help = "Check Commerce7 credentials (secret never printed) and make one test call."

    def handle(self, *args, **opts):
        env_path = Path(settings.BASE_DIR) / ".env"
        counts, raw = _env_file_lines(env_path)
        try:
            from dotenv import dotenv_values
            file_vals = dotenv_values(env_path)
        except ImportError:
            file_vals = {}

        self.stdout.write(f".env: {env_path} ({'found' if env_path.exists() else 'MISSING'})")
        for key in KEYS:
            val = os.environ.get(key, "")
            flags = []
            if not val:
                flags.append("EMPTY")
            if counts.get(key, 0) > 1:
                flags.append(f"APPEARS {counts[key]}x IN .env")
            if key in file_vals and file_vals[key] != val:
                flags.append("SHELL VARIABLE OVERRIDES .env -- open a new terminal")
            if key not in counts:
                flags.append("not in .env")
            bad = _problem_chars(val)
            if bad:
                flags.append("BAD CHARS: " + ", ".join(bad))
            shown = f"{len(val)} chars" if key == "C7_APP_SECRET" else repr(val)
            self.stdout.write(f"  {key:15} {shown:30} {'; '.join(flags)}")

        self.stdout.write("Calling GET /order?limit=1 ...")
        try:
            data = Commerce7Client.from_settings().get("order", {"limit": 1})
        except C7Error as e:
            self.stdout.write(self.style.ERROR(f"{e}\n  {e.body}"))
            if e.status_code == 401:
                self.stdout.write(
                    "  401 = C7 doesn't accept this app ID + secret pair.\n"
                    "  - C7_APP_ID is the app's ID from the Dev Center app page (lowercase,\n"
                    "    hyphenated), not its display name.\n"
                    "  - C7_APP_SECRET is the 'App Secret Key' on that same page. If it was\n"
                    "    regenerated, the old one is dead.\n"
                    "  - The app must be installed on the St. Amant tenant (Dev Center ->\n"
                    "    app -> Install / Test install).\n"
                    "  - C7_TENANT_ID is the slug in your admin URL, not a number.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"200 OK -- {data.get('total', '?')} orders visible. Run c7_probe --club."))
