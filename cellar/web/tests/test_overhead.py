"""
Regression tests for overhead allocation and abnormal loss (migration 0030).

Three behaviours carry the weight here:

  * test_below_normal_capacity_expenses_the_idle_share — a light vintage must not
    make its own wine look expensive.
  * test_old_port_stops_absorbing — the 3-year cap, without which a 2014 Port
    collects overhead for 144 months and ends up carried above realisable value.
  * test_as_of_gallons_are_historical — allocation run late must give the same
    answer as allocation run on time, or a period-locked ledger is worthless.
"""
import datetime as dt
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from cellar.models import (
    ConfigConstant, CostEntry, CostPeriod, LotCostAdjustment, Lot, OverheadPool,
    OverheadPoolPeriod, Variety, VolumeLoss, VolumeMeasurement,
)
from cellar.models.base import Program
from cellar.services import cost_ledger, costing, generator, overhead, volumes


class _Base(TestCase):
    def setUp(self):
        self.v = Variety.objects.create(name="TEST Zinfandel")
        overhead.ensure_default_pools()
        self.period = CostPeriod.objects.create(year=2025, month=10)

    def _lot(self, gallons, dollars=0, vintage=2025, when=None):
        lot = generator.create_lot(vintage, self.v, Program.TABLE,
                                   status=Lot.Status.DONE_PRIMARY)
        VolumeMeasurement.objects.create(
            lot=lot, method=VolumeMeasurement.Method.STATED,
            measured_at=when or dt.datetime(2025, 9, 1, tzinfo=dt.timezone.utc),
            volume_gal=Decimal(str(gallons)), is_booking_volume=True)
        if dollars:
            LotCostAdjustment.objects.create(
                lot=lot, kind=LotCostAdjustment.Kind.OTHER, amount=Decimal(str(dollars)),
                incurred_at=dt.date(2025, 9, 1), basis=LotCostAdjustment.Basis.ENTERED)
        return lot

    def _pool(self, key, amount):
        return OverheadPoolPeriod.objects.create(
            pool=OverheadPool.objects.get(key=key), period=self.period,
            amount=Decimal(str(amount)))


class AllocationTests(_Base):
    def test_at_normal_capacity_the_whole_pool_absorbs(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        a, b = self._lot(750), self._lot(250)
        self._pool("cellar-overhead", 1000)

        result = overhead.allocate(self.period)
        self.assertEqual(result["idle"], Decimal("0.00"))
        self.assertEqual(cost_ledger.lot_cost_posted(a), 750.00)
        self.assertEqual(cost_ledger.lot_cost_posted(b), 250.00)

    def test_below_normal_capacity_expenses_the_idle_share(self):
        """THE REGRESSION. A light vintage must not inflate its own cost/gal."""
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        lot = self._lot(400)
        self._pool("cellar-overhead", 1000)

        result = overhead.allocate(self.period)
        self.assertEqual(result["absorbed"], Decimal("400.00"))
        self.assertEqual(result["idle"], Decimal("600.00"))

        # the wine carries only its own share, not the whole pool
        self.assertEqual(cost_ledger.lot_cost_posted(lot), 400.00)
        idle = CostEntry.objects.get(category=CostEntry.Category.IDLE_CAPACITY)
        self.assertEqual(idle.amount, Decimal("600.00"))
        self.assertIsNone(idle.lot_id)          # expense, attached to no wine

    def test_above_normal_capacity_never_over_absorbs(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(5000)
        self._pool("cellar-overhead", 1000)
        result = overhead.allocate(self.period)
        self.assertEqual(result["absorbed"], Decimal("1000.00"))
        self.assertEqual(result["idle"], Decimal("0.00"))

    def test_old_port_stops_absorbing(self):
        """THE CAP. Beyond 3 vintages a lot leaves the denominator entirely."""
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        ConfigConstant.objects.create(key="overhead_absorption_max_years", value="3")
        young = self._lot(500, vintage=2025)
        old = self._lot(500, vintage=2014)

        lots = dict(overhead.absorbing_lots(self.period))
        self.assertIn(young, lots)
        self.assertNotIn(old, lots)

        self._pool("cellar-overhead", 1000)
        overhead.allocate(self.period)
        self.assertEqual(cost_ledger.lot_cost_posted(old), 0.00)
        self.assertEqual(cost_ledger.lot_cost_posted(young), 500.00)

    def test_allocation_is_idempotent(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(1000)
        self._pool("cellar-overhead", 1000)
        overhead.allocate(self.period)
        n = CostEntry.objects.count()
        second = overhead.allocate(self.period)
        self.assertEqual(second["entries"], 0)
        self.assertEqual(CostEntry.objects.count(), n)

    def test_rounding_lands_on_the_last_lot(self):
        """Three lots over $1,000 must sum to exactly $1,000, not $999.99."""
        ConfigConstant.objects.create(key="normal_capacity_gal", value="3")
        for _ in range(3):
            self._lot(1)
        self._pool("cellar-overhead", 1000)
        overhead.allocate(self.period)
        total = sum(Decimal(str(cost_ledger.lot_cost_posted(l))) for l in Lot.objects.all())
        self.assertEqual(total, Decimal("1000"))

    def test_labor_pool_posts_as_labor(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(1000)
        self._pool("production-labor", 500)
        overhead.allocate(self.period)
        self.assertTrue(CostEntry.objects.filter(category=CostEntry.Category.LABOR).exists())

    def test_allocate_with_no_amounts_raises(self):
        self._lot(100)
        with self.assertRaises(ValueError):
            overhead.allocate(self.period)


class AsOfGallonsTests(_Base):
    def test_as_of_gallons_are_historical(self):
        """Allocation run in March for October must see October's gallons."""
        lot = self._lot(1000)
        self.assertEqual(volumes.lot_balance(lot, as_of=dt.date(2025, 10, 31)),
                         Decimal("1000.0"))

        # wine leaves in December, after the period being allocated
        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("400.0"),
                                  reason="racking", occurred_at=dt.date(2025, 12, 15))

        self.assertEqual(volumes.lot_balance(lot, as_of=dt.date(2025, 10, 31)),
                         Decimal("1000.0"))                 # October unchanged
        self.assertEqual(volumes.lot_balance(lot), Decimal("600.0"))   # today's is not

    def test_lot_booked_after_month_end_does_not_absorb(self):
        late = self._lot(500, when=dt.datetime(2025, 12, 1, tzinfo=dt.timezone.utc))
        lots = dict(overhead.absorbing_lots(self.period))
        self.assertNotIn(late, lots)


class AbnormalLossTests(_Base):
    def test_normal_loss_capitalizes_into_remaining_wine(self):
        """No posting needed — cost/gal simply rises. 1000 gal @ $20k losing 35."""
        lot = self._lot(1000, dollars=20000)
        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("35.0"), reason="racking",
                                  occurred_at=dt.date(2025, 10, 5))
        self.assertEqual(costing.lot_cost_computed(lot), 20000.00)
        self.assertAlmostEqual(costing.lot_cost_per_gal(lot), 20.7254, places=3)
        overhead.post_abnormal_losses()
        self.assertFalse(CostEntry.objects.filter(
            category=CostEntry.Category.ABNORMAL_LOSS).exists())

    def test_abnormal_loss_is_taken_out_of_inventory(self):
        lot = self._lot(1000, dollars=20000)
        cost_ledger.post_all()
        self.assertEqual(cost_ledger.lot_cost_posted(lot), 20000.00)

        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("100.0"),
                                  reason="barrel failure", is_abnormal=True,
                                  occurred_at=dt.date(2025, 10, 5))
        made = overhead.post_abnormal_losses()
        self.assertEqual(len(made), 2)                       # credit + expense

        credit = CostEntry.objects.get(category=CostEntry.Category.ABNORMAL_LOSS,
                                       lot=lot)
        expense = CostEntry.objects.get(category=CostEntry.Category.ABNORMAL_LOSS,
                                        lot__isnull=True)
        self.assertLess(credit.amount, 0)
        self.assertEqual(credit.amount + expense.amount, Decimal("0.00"))  # nets out
        self.assertLess(cost_ledger.lot_cost_posted(lot), 20000.00)

    def test_abnormal_loss_posting_is_idempotent(self):
        lot = self._lot(1000, dollars=20000)
        cost_ledger.post_all()
        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("50.0"), reason="leak",
                                  is_abnormal=True, occurred_at=dt.date(2025, 10, 5))
        overhead.post_abnormal_losses()
        self.assertEqual(len(overhead.post_abnormal_losses()), 0)


class CommandTests(_Base):
    def test_seed_pools(self):
        out = StringIO()
        call_command("allocate_overhead", "--seed-pools", stdout=out)
        self.assertEqual(OverheadPool.objects.count(), 6)

    def test_allocate_dry_run_writes_nothing(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(500)
        self._pool("cellar-overhead", 1000)
        call_command("allocate_overhead", 2025, 10, "--dry-run", stdout=StringIO())
        self.assertEqual(CostEntry.objects.count(), 0)

    def test_allocate_command_reports_idle(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(500)
        self._pool("cellar-overhead", 1000)
        out = StringIO()
        call_command("allocate_overhead", 2025, 10, stdout=out)
        self.assertIn("idle $500.00", out.getvalue())

    def test_abnormal_loss_command(self):
        lot = self._lot(1000, dollars=20000)
        cost_ledger.post_all()
        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("50.0"), reason="leak",
                                  is_abnormal=True, occurred_at=dt.date(2025, 10, 5))
        out = StringIO()
        call_command("post_abnormal_losses", stdout=out)
        self.assertIn("out of inventory", out.getvalue())


from django.contrib.auth import get_user_model          # noqa: E402
from django.test import Client, override_settings       # noqa: E402
from django.urls import reverse                         # noqa: E402

WEB = override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
              "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})


@WEB
class OverheadPageTests(_Base):
    def setUp(self):
        super().setUp()
        U = get_user_model()
        self.c = Client()
        self.c.force_login(U.objects.create_user("t", password="x"))

    def test_page_renders_with_and_without_amounts(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(500)
        r = self.c.get(reverse("overhead-pools"), follow=True)
        self.assertEqual(r.status_code, 200)
        self._pool("cellar-overhead", 1000)
        r = self.c.get(reverse("overhead-pools") + f"?period={self.period.pk}", follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Idle")

    def test_saving_amounts(self):
        ConfigConstant.objects.create(key="normal_capacity_gal", value="1000")
        self._lot(500)
        pool = OverheadPool.objects.get(key="utilities")
        r = self.c.post(reverse("overhead-pools"),
                        {"period": self.period.pk, f"amount:{pool.pk}": "425.50"}, follow=True)
        self.assertEqual(r.status_code, 200)
        pp = OverheadPoolPeriod.objects.get(pool=pool, period=self.period, voided_at__isnull=True)
        self.assertEqual(pp.amount, Decimal("425.50"))

    def test_closed_period_rejects_edits(self):
        self.period.status = CostPeriod.Status.CLOSED
        self.period.save()
        pool = OverheadPool.objects.get(key="utilities")
        r = self.c.post(reverse("overhead-pools"),
                        {"period": self.period.pk, f"amount:{pool.pk}": "100"}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(OverheadPoolPeriod.objects.filter(pool=pool).exists())
