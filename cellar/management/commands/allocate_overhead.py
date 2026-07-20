"""Allocate a month's overhead pools across bulk gallons.

    python manage.py allocate_overhead 2025 10 --dry-run
    python manage.py allocate_overhead 2025 10
    python manage.py allocate_overhead --seed-pools

Idempotent per pool-month: an already-allocated pool is skipped, not re-posted.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Allocate monthly overhead pools to lots by month-end bulk gallons."

    def add_arguments(self, parser):
        parser.add_argument("year", type=int, nargs="?")
        parser.add_argument("month", type=int, nargs="?")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--seed-pools", action="store_true",
                            help="create the six default pools and exit")

    def handle(self, *args, **opts):
        from cellar.models import CostPeriod
        from cellar.services import overhead

        if opts["seed_pools"]:
            made = overhead.ensure_default_pools()
            self.stdout.write(self.style.SUCCESS(
                f"Created {len(made)} pool(s): {', '.join(p.name for p in made) or '(all existed)'}"))
            return

        if not opts["year"] or not opts["month"]:
            self.stderr.write(self.style.ERROR("Give a year and month, or --seed-pools."))
            return

        period = CostPeriod.objects.filter(year=opts["year"], month=opts["month"]).first()
        if period is None:
            self.stderr.write(self.style.ERROR(
                f"No period {opts['year']}-{opts['month']:02d}. Run post_costs first."))
            return

        plan = overhead.preview(period)
        self.stdout.write(f"\n{period.label}  (gallons as of {plan['as_of']})")
        self.stdout.write(
            f"  absorbing {plan['absorbing_gallons']} gal across {plan['lot_count']} lot(s); "
            f"normal capacity {plan['normal_capacity']} gal; "
            f"absorption {plan['absorption_ratio'] * 100:.1f}%")
        if plan["absorption_ratio"] < 1:
            self.stdout.write(self.style.WARNING(
                "  Below normal capacity — the unabsorbed share is expensed as idle capacity, "
                "not loaded onto the wine that happens to exist."))
        self.stdout.write("")
        for p in plan["pools"]:
            flag = "  (already allocated)" if p["already"] else ""
            self.stdout.write(
                f"  {p['pool'].name:<26} ${p['amount']:>10}  absorbed ${p['absorbed']:>10}  "
                f"idle ${p['idle']:>9}{flag}")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN — nothing written."))
            return

        try:
            result = overhead.allocate(period)
        except ValueError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return
        self.stdout.write(self.style.SUCCESS(
            f"\nPosted {result['entries']} entries. "
            f"Absorbed ${result['absorbed']}, idle ${result['idle']}."))
