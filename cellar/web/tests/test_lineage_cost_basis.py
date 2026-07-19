"""Regression tests for the lineage cost-basis snapshot (migration 0027).

THE BUG THIS PINS DOWN
----------------------
Cost inheritance used to be computed live:

    inherited = lot_cost(parent) / cost_basis_volume(parent) * edge.volume_gal

On a WHOLE_BLEND the parent's entire balance moves to the child, so by the time
anyone looked, cost_basis_volume(parent) was 0. The `if pv:` guard then skipped
the edge outright and the blended child inherited $0.00 — a blend of two fully
costed lots reported no fruit cost at all.

test_whole_blend_inherits_full_parent_cost is the one that failed before 0027.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from cellar.models import (
    ConfigConstant, Lot, LotCostAdjustment, LotLineage, Variety, VolumeMeasurement,
)
from cellar.models.base import Program
from cellar.services import blending, costing, generator


def _book(lot, gallons, when=None):
    """Give a lot a booking gauge so it has a volume basis."""
    return VolumeMeasurement.objects.create(
        lot=lot, method=VolumeMeasurement.Method.STATED,
        measured_at=when or timezone.now(),
        volume_gal=Decimal(str(gallons)), is_booking_volume=True)


def _cost(lot, dollars, when=None):
    """Put a known direct cost on a lot without needing weigh tags."""
    return LotCostAdjustment.objects.create(
        lot=lot, kind=LotCostAdjustment.Kind.OTHER,
        amount=Decimal(str(dollars)),
        incurred_at=(when or timezone.now()).date(),
        basis=LotCostAdjustment.Basis.ENTERED)


class LineageCostBasisTests(TestCase):
    def setUp(self):
        self.zin = Variety.objects.create(name="Zinfandel")
        self.bar = Variety.objects.create(name="Barbera")

    def _lot(self, variety, gallons, dollars):
        lot = generator.create_lot(2025, variety, Program.TABLE,
                                   status=Lot.Status.DONE_PRIMARY)
        _book(lot, gallons)
        _cost(lot, dollars)
        return lot

    # ------------------------------------------------------------------ core
    def test_whole_blend_inherits_full_parent_cost(self):
        """THE REGRESSION. Parent's whole balance moves; child must inherit all of it."""
        parent = self._lot(self.zin, 1000, 20000)      # $20.00/gal
        child = self._lot(self.bar, 500, 5000)         # $10.00/gal

        self.assertEqual(costing.lot_cost_per_gal(parent), 20.0)

        edge = blending.blend(parent, child,
                              blended_at=timezone.localdate(),
                              kind=LotLineage.Relationship.WHOLE_BLEND)

        # snapshot frozen at the PRE-transfer rate, not the post-transfer zero
        self.assertEqual(edge.cost_per_gal_snapshot, Decimal("20.0000"))
        self.assertEqual(edge.volume_gal, Decimal("1000.0"))
        self.assertEqual(edge.occurred_at, timezone.localdate())

        # child now holds 1500 gal and $25,000 — its own $5k plus the parent's $20k
        self.assertEqual(costing.lot_cost(child), 25000.00)
        self.assertAlmostEqual(costing.lot_cost_per_gal(child), 16.6667, places=3)

        # and the parent, drained, is worth nothing
        self.assertEqual(blending.source_balance(parent), Decimal("0.0"))

    def test_partial_blend_splits_cost_pro_rata(self):
        parent = self._lot(self.zin, 1000, 20000)      # $20.00/gal
        child = self._lot(self.bar, 500, 5000)

        edge = blending.blend(parent, child,
                              blended_at=timezone.localdate(),
                              kind=LotLineage.Relationship.PARTIAL_BLEND,
                              volume_gal=250)

        self.assertEqual(edge.cost_per_gal_snapshot, Decimal("20.0000"))
        # child: $5,000 own + 250 gal × $20 = $10,000
        self.assertEqual(costing.lot_cost(child), 10000.00)
        # parent keeps 750 gal; its own direct cost is untouched by the edge
        self.assertEqual(blending.source_balance(parent), Decimal("750.0"))

    def test_snapshot_is_immune_to_later_parent_cost(self):
        """A cost booked on the parent AFTER the blend must not restate the child."""
        parent = self._lot(self.zin, 1000, 20000)
        child = self._lot(self.bar, 500, 5000)

        blending.blend(parent, child, blended_at=timezone.localdate(),
                       kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=250)
        before = costing.lot_cost(child)

        _cost(parent, 9999)                            # later overhead on the parent
        self.assertEqual(costing.lot_cost(child), before)

    def test_normal_loss_raises_cost_per_gallon(self):
        """1,000 gal @ $20,000 losing 35 gal is still $20,000 over 965 gal."""
        from cellar.models import VolumeLoss
        lot = self._lot(self.zin, 1000, 20000)
        self.assertEqual(costing.lot_cost_per_gal(lot), 20.0)

        VolumeLoss.objects.create(lot=lot, volume_gal=Decimal("35.0"),
                                  reason="racking", occurred_at=timezone.now())

        self.assertEqual(costing.lot_cost(lot), 20000.00)          # dollars unchanged
        self.assertAlmostEqual(costing.lot_cost_per_gal(lot), 20.7254, places=3)

    # ------------------------------------------------------- helper behaviour
    def test_to_business_date_handles_datetime_subclass(self):
        """datetime is a subclass of date — the guard must test datetime FIRST."""
        import datetime as dt
        d = dt.date(2025, 7, 4)
        self.assertEqual(costing.to_business_date(d), d)
        self.assertEqual(
            costing.to_business_date(dt.datetime(2025, 7, 4, 13, 30)), d)
        self.assertIsNone(costing.to_business_date(None))
        self.assertNotIsInstance(
            costing.to_business_date(dt.datetime(2025, 7, 4, 13, 30)), dt.datetime)

    def test_edge_is_append_only_after_creation(self):
        parent = self._lot(self.zin, 1000, 20000)
        child = self._lot(self.bar, 500, 5000)
        edge = blending.blend(parent, child, blended_at=timezone.localdate(),
                              kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=100)
        edge.cost_per_gal_snapshot = Decimal("1.0000")
        with self.assertRaises(ValueError):
            edge.save()

    # --------------------------------------------------------------- backfill
    def test_backfill_prices_a_legacy_edge(self):
        """An edge written without a snapshot gets one, at the pre-transfer rate."""
        from django.core.management import call_command
        from io import StringIO

        parent = self._lot(self.zin, 1000, 20000)
        child = self._lot(self.bar, 500, 5000)

        # simulate a pre-0027 row: edge exists, both new fields null
        edge = blending.blend(parent, child, blended_at=timezone.localdate(),
                              kind=LotLineage.Relationship.WHOLE_BLEND)
        LotLineage.objects.filter(pk=edge.pk).update(
            cost_per_gal_snapshot=None, occurred_at=None)

        call_command("backfill_lineage_cost", stdout=StringIO())

        edge.refresh_from_db()
        self.assertEqual(edge.cost_per_gal_snapshot, Decimal("20.0000"))
        self.assertIsNotNone(edge.occurred_at)
        self.assertEqual(costing.lot_cost(child), 25000.00)

    def test_backfill_dry_run_writes_nothing(self):
        from django.core.management import call_command
        from io import StringIO

        parent = self._lot(self.zin, 1000, 20000)
        child = self._lot(self.bar, 500, 5000)
        edge = blending.blend(parent, child, blended_at=timezone.localdate(),
                              kind=LotLineage.Relationship.WHOLE_BLEND)
        LotLineage.objects.filter(pk=edge.pk).update(cost_per_gal_snapshot=None)

        call_command("backfill_lineage_cost", "--dry-run", stdout=StringIO())

        edge.refresh_from_db()
        self.assertIsNone(edge.cost_per_gal_snapshot)
