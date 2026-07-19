"""
Regression tests for the posted cost ledger (migration 0029).

Two behaviours matter most here:

  * test_closed_period_cannot_be_restated — the whole reason the ledger is stored
    rather than derived like compliance_ledger.py.
  * test_blend_posts_both_sides — the parent is finally CREDITED for wine it gave
    away. Under the live computation it never was, so summing every lot
    double-counted blended wine and overstated total inventory value.
"""
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cellar.models import (
    CostEntry, CostPeriod, Lot, LotCostAdjustment, LotLineage, Variety, VolumeMeasurement,
)
from cellar.models.base import Program
from cellar.services import blending, cost_ledger, costing, generator

WEB = override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
              "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})


class _Base(TestCase):
    def setUp(self):
        self.v1 = Variety.objects.create(name="TEST Zinfandel")
        self.v2 = Variety.objects.create(name="TEST Barbera")

    def _lot(self, variety, gallons, dollars, when=None):
        lot = generator.create_lot(2025, variety, Program.TABLE, status=Lot.Status.DONE_PRIMARY)
        VolumeMeasurement.objects.create(
            lot=lot, method=VolumeMeasurement.Method.STATED, measured_at=timezone.now(),
            volume_gal=Decimal(str(gallons)), is_booking_volume=True)
        LotCostAdjustment.objects.create(
            lot=lot, kind=LotCostAdjustment.Kind.OTHER, amount=Decimal(str(dollars)),
            incurred_at=when or timezone.localdate(), basis=LotCostAdjustment.Basis.ENTERED)
        return lot


class PostingTests(_Base):
    def test_posting_is_idempotent(self):
        lot = self._lot(self.v1, 1000, 20000)
        first = cost_ledger.post_all()
        self.assertEqual(first["entries"], 1)
        second = cost_ledger.post_all()
        self.assertEqual(second["entries"], 0)          # nothing new the second time
        self.assertEqual(CostEntry.objects.filter(lot=lot).count(), 1)

    def test_posted_ledger_becomes_the_source_of_truth(self):
        lot = self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        self.assertEqual(costing.lot_cost(lot), 20000.00)
        self.assertEqual(cost_ledger.lot_cost_posted(lot), 20000.00)

    def test_unposted_lot_falls_back_to_live_computation(self):
        """Day one: nothing posted, Cost tiles must still show a number."""
        lot = self._lot(self.v1, 500, 3000)
        self.assertFalse(cost_ledger.has_postings(lot))
        self.assertEqual(costing.lot_cost(lot), 3000.00)

    def test_blend_posts_both_sides(self):
        """THE REGRESSION. Parent is credited for what it gave away."""
        parent = self._lot(self.v1, 1000, 20000)
        child = self._lot(self.v2, 500, 5000)
        blending.blend(parent, child, blended_at=timezone.localdate(),
                       kind=LotLineage.Relationship.WHOLE_BLEND)
        cost_ledger.post_all()

        out = CostEntry.objects.get(lot=parent, category=CostEntry.Category.TRANSFER_OUT)
        into = CostEntry.objects.get(lot=child, category=CostEntry.Category.TRANSFER_IN)
        self.assertEqual(out.amount, Decimal("-20000.00"))
        self.assertEqual(into.amount, Decimal("20000.00"))

        # parent drained to zero, child holds everything, winery total is unchanged
        self.assertEqual(cost_ledger.lot_cost_posted(parent), 0.00)
        self.assertEqual(cost_ledger.lot_cost_posted(child), 25000.00)
        self.assertEqual(cost_ledger.wip_total(), Decimal("25000.00"))

    def test_expense_categories_are_excluded_from_lot_cost(self):
        lot = self._lot(self.v1, 100, 1000)
        cost_ledger.post_all()
        period = cost_ledger.period_for(timezone.localdate())
        CostEntry.objects.create(lot=None, period=period,
                                 category=CostEntry.Category.SHRINKAGE,
                                 amount=Decimal("-250.00"), occurred_at=timezone.localdate())
        self.assertEqual(cost_ledger.lot_cost_posted(lot), 1000.00)
        s = cost_ledger.period_summary(period)
        self.assertEqual(s["expense"], Decimal("-250.00"))
        self.assertEqual(s["capitalized"], Decimal("1000.00"))


class ClosedPeriodTests(_Base):
    def test_closed_period_cannot_be_restated(self):
        """THE POINT OF A STORED LEDGER. Void the source; the posted cost stands."""
        lot = self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        period = cost_ledger.period_for(timezone.localdate())
        cost_ledger.close_period(period)

        adj = lot.cost_adjustments.first()
        adj.voided_at = timezone.now()
        adj.save()

        self.assertEqual(costing.lot_cost_computed(lot), 0.00)   # live view collapses
        self.assertEqual(costing.lot_cost(lot), 20000.00)        # posted figure holds

    def test_cost_into_a_closed_month_defers_with_a_mandatory_note(self):
        import datetime as dt
        march = CostPeriod.objects.create(year=2025, month=3,
                                          status=CostPeriod.Status.CLOSED,
                                          closed_at=timezone.now())
        CostPeriod.objects.create(year=2025, month=4)

        lot = self._lot(self.v1, 100, 500, when=dt.date(2025, 3, 15))
        cost_ledger.post_all()

        e = CostEntry.objects.get(lot=lot, category=CostEntry.Category.ADJUSTMENT)
        self.assertNotEqual(e.period_id, march.pk)
        self.assertEqual(e.period.label, "2025-04")
        self.assertIn("2025-03", e.deferred_note)
        self.assertIn("closed", e.deferred_note.lower())
        self.assertTrue(e.deferred_note)                          # never silent

    def test_close_refuses_when_reconciliation_is_out(self):
        lot = self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        LotCostAdjustment.objects.create(
            lot=lot, kind=LotCostAdjustment.Kind.OTHER, amount=Decimal("500"),
            incurred_at=timezone.localdate(), basis=LotCostAdjustment.Basis.ENTERED)
        period = cost_ledger.period_for(timezone.localdate())
        with self.assertRaises(ValueError) as cm:
            cost_ledger.close_period(period)
        self.assertIn("reconcile", str(cm.exception))

    def test_close_with_force_overrides(self):
        lot = self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        LotCostAdjustment.objects.create(
            lot=lot, kind=LotCostAdjustment.Kind.OTHER, amount=Decimal("500"),
            incurred_at=timezone.localdate(), basis=LotCostAdjustment.Basis.ENTERED)
        period = cost_ledger.period_for(timezone.localdate())
        cost_ledger.close_period(period, force=True)
        period.refresh_from_db()
        self.assertEqual(period.status, CostPeriod.Status.CLOSED)

    def test_cannot_close_twice(self):
        self._lot(self.v1, 100, 100)
        cost_ledger.post_all()
        period = cost_ledger.period_for(timezone.localdate())
        cost_ledger.close_period(period)
        with self.assertRaises(ValueError):
            cost_ledger.close_period(period)


class ReconcileTests(_Base):
    def test_clean_after_posting(self):
        self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        self.assertTrue(all(r["ok"] for r in cost_ledger.reconcile()))

    def test_blended_parent_still_reconciles(self):
        """TRANSFER_OUT is an expected difference and must not read as drift."""
        parent = self._lot(self.v1, 1000, 20000)
        child = self._lot(self.v2, 500, 5000)
        blending.blend(parent, child, blended_at=timezone.localdate(),
                       kind=LotLineage.Relationship.WHOLE_BLEND)
        cost_ledger.post_all()
        rows = {r["lot"].pk: r for r in cost_ledger.reconcile()}
        self.assertTrue(rows[parent.pk]["ok"], rows[parent.pk])
        self.assertTrue(rows[child.pk]["ok"], rows[child.pk])

    def test_drift_is_detected(self):
        lot = self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        LotCostAdjustment.objects.create(
            lot=lot, kind=LotCostAdjustment.Kind.OAK, amount=Decimal("1234.00"),
            incurred_at=timezone.localdate(), basis=LotCostAdjustment.Basis.ENTERED)
        row = next(r for r in cost_ledger.reconcile() if r["lot"].pk == lot.pk)
        self.assertFalse(row["ok"])
        self.assertEqual(row["diff"], Decimal("-1234.00"))


class CommandTests(_Base):
    def test_post_costs_dry_run_writes_nothing(self):
        self._lot(self.v1, 100, 900)
        call_command("post_costs", "--dry-run", stdout=StringIO())
        self.assertEqual(CostEntry.objects.count(), 0)

    def test_post_and_reconcile_commands(self):
        self._lot(self.v1, 100, 900)
        out = StringIO()
        call_command("post_costs", stdout=out)
        self.assertIn("Posted 1", out.getvalue())
        out = StringIO()
        call_command("cost_reconcile", stdout=out)
        self.assertIn("reconcile", out.getvalue())

    def test_close_command(self):
        self._lot(self.v1, 100, 900)
        call_command("post_costs", stdout=StringIO())
        today = timezone.localdate()
        out = StringIO()
        call_command("close_cost_period", today.year, today.month, stdout=out)
        self.assertIn("Closed", out.getvalue())


@WEB
class CostPageTests(_Base):
    def setUp(self):
        super().setUp()
        U = get_user_model()
        self.c = Client()
        self.c.force_login(U.objects.create_user("t", password="x"))

    def test_pages_render(self):
        self._lot(self.v1, 1000, 20000)
        cost_ledger.post_all()
        r = self.c.get(reverse("cost-periods"), follow=True)
        self.assertEqual(r.status_code, 200)
        p = CostPeriod.objects.first()
        r = self.c.get(reverse("cost-period", args=[p.pk]), follow=True)
        self.assertEqual(r.status_code, 200)

    def test_page_renders_with_nothing_posted(self):
        r = self.c.get(reverse("cost-periods"), follow=True)
        self.assertEqual(r.status_code, 200)
