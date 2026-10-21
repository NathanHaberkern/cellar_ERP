"""Diagnose FedEx credentials without printing them.

    python manage.py fedex_check

Shows, for each FEDEX_* setting: its length, whether it carries quotes or
stray spaces, whether it appears more than once in .env, and whether the
running shell overrides the .env value (python-dotenv never overwrites a
variable already set in the shell). Then asks FedEx for an OAuth token and
prints FedEx's verdict. Values themselves are never printed.
"""
import os
import unicodedata
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from shipping.carriers.fedex import BASE_URLS

KEYS = ("FEDEX_ENV", "FEDEX_CLIENT_ID", "FEDEX_CLIENT_SECRET", "FEDEX_ACCOUNT_NUMBER")


def _env_file_lines(path):
    counts, raw = {}, {}
    if not path.exists():
        return counts, raw
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        counts[k] = counts.get(k, 0) + 1
        raw[k] = v
    return counts, raw


def _odd_chars(val):
    """Describe every character that isn't a plain ASCII letter or digit.

    FedEx keys and secrets are plain alphanumerics, so anything else -- a curly
    quote, a zero-width space, a non-breaking space picked up by copy/paste --
    is the bug. Reports position and Unicode name, never the value itself.
    """
    out = []
    for i, ch in enumerate(val, 1):
        if ch.isascii() and ch.isalnum():
            continue
        name = unicodedata.name(ch, f"U+{ord(ch):04X}")
        out.append(f"pos {i}: {name}")
    return out


class Command(BaseCommand):
    help = "Check FedEx credentials (lengths and problems only) and request a sandbox token."

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
            if raw.get(key, "").strip()[:1] in ("'", '"'):
                flags.append("quoted in .env (ok, dotenv strips them)")
            if val != val.strip():
                flags.append("LEADING/TRAILING SPACES")
            if val[:1] in ("'", '"') or val[-1:] in ("'", '"'):
                flags.append("QUOTE CHARACTERS IN VALUE")
            if counts.get(key, 0) > 1:
                flags.append(f"APPEARS {counts[key]}x IN .env")
            if key in file_vals and file_vals[key] != val:
                flags.append("SHELL VARIABLE OVERRIDES .env -- open a new terminal")
            if key not in counts:
                flags.append("not in .env")
            if key != "FEDEX_ENV":
                odd = _odd_chars(val)
                if odd:
                    flags.append("NON-ALPHANUMERIC: " + ", ".join(odd))
            shown = val if key == "FEDEX_ENV" else f"{len(val)} chars"
            self.stdout.write(f"  {key:22} {shown:12} {'; '.join(flags)}")

        conf = settings.SHIPPING["FEDEX"]
        base = BASE_URLS.get(conf["ENV"])
        if not base:
            self.stdout.write(self.style.ERROR("FEDEX_ENV must be sandbox or production."))
            return
        self.stdout.write(f"Requesting token from {base} ...")
        try:
            r = requests.post(f"{base}/oauth/token", timeout=30, data={
                "grant_type": "client_credentials",
                "client_id": conf["CLIENT_ID"], "client_secret": conf["CLIENT_SECRET"]})
        except requests.RequestException as e:
            self.stdout.write(self.style.ERROR(f"Network error: {e}"))
            return
        if r.status_code == 200 and "access_token" in r.text:
            self.stdout.write(self.style.SUCCESS(
                "200 OK -- FedEx accepted these credentials. Run fedex_test_label."))
        else:
            self.stdout.write(self.style.ERROR(f"{r.status_code}: {r.text[:300]}"))
