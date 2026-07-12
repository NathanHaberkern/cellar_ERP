"""
Book a year of estimated angel's share on long-aged port barrels.

    python manage.py accrue_evaporation --year 2025          # dry run
    python manage.py accrue_evaporation --year 2025 --yes    # book it

Dry run by default, like reset_transactional — this writes VolumeLoss rows that
land on 5120.17 line A30, so you see the list before it goes on a filing.
Idempotent: re-running a year that's already booked does nothing.
"""
from datetime import date

from django.core.management.base import BaseCommand

from cellar.services import evaporation


class Command(BaseCommand):
    help = "Accrue estimated evaporation on port barrels aged >5 years without topping."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=date.today().year - 1)
        parser.add_argument("--from-year", type=int, default=None,
                            help="Catch up: book every year from here through --year. "
                                 "A 2015 port barrel needs 2020…2025 booked, not just 2025.")
        parser.add_argument("--yes", action="store_true",
                            help="Actually book the losses. Without this it is a dry run.")

    def handle(self, *args, **opts):
        year, commit = opts["year"], opts["yes"]
        start = opts["from_year"] or year
        if start > year:
            self.stderr.write("--from-year must be <= --year")
            return
        if start < year:
            for y in range(start, year):
                r = evaporation.accrue(y, commit=commit)
                booked = sum(row["loss"] for row in r["plan"].values()
                             if not row["already_booked"])
                verb = "booked" if commit else "would book"
                self.stdout.write(f"  {y}: {verb} {booked} gal across "
                                  f"{len(r['plan'])} lot(s)")

        result = evaporation.accrue(year, commit=commit)
        rows = result["plan"]

        if not rows:
            self.stdout.write(self.style.SUCCESS(
                f"No port barrels over {evaporation.MIN_AGE_YEARS} years old and untopped. "
                f"Nothing to accrue for {year}."))
            return

        rate = int(evaporation.ANNUAL_RATE * 100)
        self.stdout.write(self.style.WARNING(
            f"Angel's share accrual for {year} — {rate}%/yr:"))
        total = 0
        for row in rows.values():
            flag = "  (already booked)" if row["already_booked"] else ""
            self.stdout.write(
                f"  {row['lot'].code:<14} {row['barrels']:>3} bbl  "
                f"{row['on_books']:>8} gal on books  →  −{row['loss']} gal{flag}")
            if not row["already_booked"]:
                total += row["loss"]

        if not commit:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — {total} gal would be booked as VolumeLoss (5120.17 line A30).\n"
                "Re-run with --yes to book it. Trued up to the real gauge at barrel-down."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"\nBooked {len(result['created'])} accruals totalling {total} gal."))
