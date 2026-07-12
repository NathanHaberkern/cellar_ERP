"""
Import the 2025 Port fortifications from a CSV, and reconcile them against the
filed 5120.17s before writing anything.

    python manage.py import_ports ports_2025.csv            # dry run + reconcile
    python manage.py import_ports ports_2025.csv --yes

WHY A CSV AND NOT A TRANSCRIPTION
---------------------------------
The cellar notes are image-only scans of handwriting. Rather than have anything
guess at a proof gallon and put it on a federal form, the numbers get typed once
and then checked — hard — against three independent figures that already exist in
the filed reports and cannot all be wrong at the same time:

  1. BASE WINE BY MONTH.  Every initial fortification's base wine (finished − spirit)
     must sum, per month, to what was filed on line 2:
         September ...... 365.10 gal
         October ..... 2,759.00 gal
     (Filed in col (b); the classification was wrong, the totals were not.)

  2. SPIRIT BY MONTH.  Proof gallons drawn must sum, per month, to Part III line 5:
         June ......... 112.40 PG      (the spring alcohol adjustment)
         September .... 129.88 PG
         October ...... 700.54 PG

  3. PROOF.  October's filed figures imply the spirit was 173.40 proof
     (700.54 PG ÷ (3,163 − 2,759) WG). A row whose proof is materially off will
     throw check 1 out even when the gallons look right.

If a typo slips in, at least one of these fails and says which lot and by how much.
Nothing is written unless --yes, and --yes still refuses on a failed reconciliation
unless you pass --force.

CSV COLUMNS
    lot_code          25TRPORT1        (blank → generated from variety + program)
    variety           Tinta Roriz
    kind              initial | adjustment
    on_skins_date     2025-10-04       (adjustment: the racking date)
    booked_at         2025-10-22       volume-determination date
    pg_drawn          700.54
    spirit_proof      173.4            (blank → the HPGS blended proof)
    finished_wg       3163             the gauge AFTER fortification (T)
    base_wg                            adjustment ONLY — the gauge going IN
    target_abv        19.5             (blank → tax class from expected_tax_class)
    expected_tax_class b
    notes             free text
"""
import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from cellar.models import FortificationEvent, Lot, Program, Variety
from cellar.services import fortification as fort_svc
from cellar.services import generator

CENT = Decimal("0.01")

# --- the filed figures. These are the ground truth, read off the 2025 reports. ---
FILED_BASE_BY_MONTH = {9: Decimal("365.10"), 10: Decimal("2759.00")}
FILED_PG_BY_MONTH = {6: Decimal("112.40"), 9: Decimal("129.88"), 10: Decimal("700.54")}
IMPLIED_PROOF = Decimal("173.40")
TOLERANCE = Decimal("1.0")          # gallons / proof gallons


def _dec(v):
    v = (v or "").strip()
    return Decimal(v) if v else None


def _date(v):
    v = (v or "").strip()
    if not v:
        return None
    return datetime.strptime(v, "%Y-%m-%d").date()


class Command(BaseCommand):
    help = "Import 2025 Port fortifications from CSV and reconcile to the filed 5120.17s."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--yes", action="store_true", help="Actually write.")
        parser.add_argument("--force", action="store_true",
                            help="Write even if the reconciliation fails. Think hard.")

    def handle(self, *args, **opts):
        with open(opts["csv_path"], newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh)
                    if any((v or "").strip() for v in r.values())]

        if not rows:
            self.stderr.write("Empty CSV.")
            return

        parsed, errors = [], []
        for i, r in enumerate(rows, start=2):
            try:
                parsed.append(self._parse(r))
            except Exception as e:  # noqa: BLE001
                errors.append(f"  line {i}: {e}")

        if errors:
            self.stdout.write(self.style.ERROR("Could not read these rows:"))
            for e in errors:
                self.stdout.write(e)
            return

        self._show(parsed)
        ok = self._reconcile(parsed)

        if not opts["yes"]:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing written. Re-run with --yes."))
            return
        if not ok and not opts["force"]:
            self.stdout.write(self.style.ERROR(
                "\nRECONCILIATION FAILED — nothing written. Fix the CSV, or pass --force "
                "if you are certain the filed reports are the thing that's wrong."))
            return

        self._write(parsed)

    # ------------------------------------------------------------------ parse
    def _parse(self, r):
        kind = (r.get("kind") or "initial").strip().lower()
        if kind not in ("initial", "adjustment"):
            raise ValueError(f"kind must be 'initial' or 'adjustment', got '{kind}'")

        vname = (r.get("variety") or "").strip()
        variety = Variety.objects.filter(name__iexact=vname).first() if vname else None
        if variety is None and kind == "initial":
            raise ValueError(f"unknown variety '{vname}' — seed the variety catalog first")

        pg = _dec(r.get("pg_drawn"))
        finished = _dec(r.get("finished_wg"))
        if pg is None or finished is None:
            raise ValueError("pg_drawn and finished_wg are both required")

        proof = _dec(r.get("spirit_proof")) or IMPLIED_PROOF
        spirit_wg = (pg * 100 / proof).quantize(CENT)
        base = _dec(r.get("base_wg"))
        if kind == "initial":
            base = (finished - spirit_wg).quantize(Decimal("0.1"))
        elif base is None:
            raise ValueError("an adjustment needs base_wg — the gauge going in")

        booked = _date(r.get("booked_at"))
        if booked is None:
            raise ValueError("booked_at is required (the volume-determination date)")

        return {
            "lot_code": (r.get("lot_code") or "").strip(),
            "variety": variety,
            "kind": kind,
            "on_skins": _date(r.get("on_skins_date")) or booked,
            "booked_at": booked,
            "pg": pg,
            "proof": proof,
            "spirit_wg": spirit_wg,
            "finished": finished,
            "base": base,
            "target_abv": _dec(r.get("target_abv")),
            "tax_class": (r.get("expected_tax_class") or "b").strip() or "b",
            "notes": (r.get("notes") or "").strip(),
        }

    # ------------------------------------------------------------------- show
    def _show(self, parsed):
        self.stdout.write(self.style.WARNING("Port fortifications read from CSV:\n"))
        hdr = (f"  {'lot':<12}{'kind':<11}{'booked':<12}{'PG':>9}{'proof':>8}"
               f"{'spirit WG':>11}{'base':>10}{'finished':>10}")
        self.stdout.write(hdr)
        self.stdout.write("  " + "-" * (len(hdr) - 2))
        for p in parsed:
            self.stdout.write(
                f"  {(p['lot_code'] or '(new)'):<12}{p['kind']:<11}"
                f"{str(p['booked_at']):<12}{p['pg']:>9}{p['proof']:>8}"
                f"{p['spirit_wg']:>11}{p['base']:>10}{p['finished']:>10}")

    # -------------------------------------------------------------- reconcile
    def _reconcile(self, parsed):
        base_by_month = defaultdict(Decimal)
        pg_by_month = defaultdict(Decimal)
        for p in parsed:
            if p["kind"] == "initial":
                base_by_month[p["booked_at"].month] += p["base"]
            pg_by_month[p["booked_at"].month] += p["pg"]

        ok = True
        self.stdout.write(self.style.WARNING(
            "\nReconciliation against the filed 2025 reports:\n"))

        self.stdout.write("  BASE WINE (Part I line 2 — filed in col (b), should be col (a)):")
        for m, filed in sorted(FILED_BASE_BY_MONTH.items()):
            got = base_by_month.get(m, Decimal("0"))
            delta = (got - filed).quantize(Decimal("0.01"))
            good = abs(delta) <= TOLERANCE
            ok &= good
            mark = "OK " if good else "OFF"
            name = date(2025, m, 1).strftime("%B")
            self.stdout.write(
                f"    {name:<11} yours {got:>10}   filed {filed:>10}   "
                f"Δ {delta:>8}  {mark}")

        self.stdout.write("\n  SPIRIT DRAWN (Part III line 5, proof gallons):")
        for m in sorted(set(FILED_PG_BY_MONTH) | set(pg_by_month)):
            filed = FILED_PG_BY_MONTH.get(m, Decimal("0"))
            got = pg_by_month.get(m, Decimal("0"))
            delta = (got - filed).quantize(Decimal("0.01"))
            good = abs(delta) <= TOLERANCE
            ok &= good
            mark = "OK " if good else "OFF"
            name = date(2025, m, 1).strftime("%B")
            self.stdout.write(
                f"    {name:<11} yours {got:>10}   filed {filed:>10}   "
                f"Δ {delta:>8}  {mark}")

        if ok:
            self.stdout.write(self.style.SUCCESS(
                "\n  Every month ties. The transcription is consistent with what you filed."))
        else:
            self.stdout.write(self.style.ERROR(
                "\n  A month does not tie. Either a figure was mistyped, or the spirit proof "
                "for that shipment is not 173.4. Check the note against the row above before "
                "anything is written."))
        return ok

    # ------------------------------------------------------------------ write
    @transaction.atomic
    def _write(self, parsed):
        made = 0
        for p in parsed:
            lot = None
            if p["lot_code"]:
                lot = next((l for l in Lot.objects.all() if l.code == p["lot_code"]), None)

            if lot is None:
                if p["kind"] == "adjustment":
                    # An alcohol adjustment re-fortifies wine that is already in bond.
                    # There is nothing to create — if we can't find the lot, the row is
                    # wrong, and minting a new lot would invent 6,823 gallons of port.
                    raise ValueError(
                        f"Adjustment row names lot '{p['lot_code'] or '(blank)'}', which "
                        f"does not exist. An alcohol adjustment re-fortifies wine already "
                        f"in bond — give the lot_code of the wine you racked.")
                if p["variety"] is None:
                    raise ValueError("An initial fortification needs a variety.")
                lot = generator.create_lot(2025, p["variety"], Program.PORT,
                                           status=Lot.Status.DONE_PRIMARY)

            if p["kind"] == "initial":
                fort_svc.fortify(
                    lot,
                    fortified_on_skins_date=p["on_skins"],
                    booked_at=p["booked_at"],
                    proof_gallons_drawn=p["pg"],
                    finished_wg=p["finished"],
                    spirit_proof=p["proof"],
                    expected_tax_class=p["tax_class"])
            else:
                fort_svc.adjust_alcohol(
                    lot,
                    adjusted_at=p["booked_at"],
                    proof_gallons_drawn=p["pg"],
                    base_wg=p["base"],
                    finished_wg=p["finished"],
                    spirit_proof=p["proof"],
                    base_tax_class=p["tax_class"],
                    expected_tax_class=p["tax_class"])
            made += 1
            self.stdout.write(f"  wrote {lot.code}  ({p['kind']})")

        self.stdout.write(self.style.SUCCESS(
            f"\n{made} fortifications written. "
            f"{FortificationEvent.objects.filter(voided_at__isnull=True).count()} on the books."))
