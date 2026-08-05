"""Partial-fill classification, barrel-level blend genealogy, and reservations.

Three things under test, in the order they depend on each other:

  1. A PARTIAL_FILL topping writes a distinct lineage type, is excluded from the
     5-gallon routine-topping flag, still counts toward composition, and — the
     reason the type exists at all — DOES produce a Part X change-of-tax-class
     entry when it crosses classes, where a routine top does not.
  2. blend(sources=...) records which barrels fed the edge, and refuses detail
     that contradicts the edge.
  3. A barrel reservation is satisfied by the blend that names its barrel, and
     deliberately NOT by a partial blend that doesn't say which barrels moved.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from cellar.models import (AgingPlacement, BookToBond, Container, LineageSource,
                           LotLineage, LotReservation, ToppingEvent, ToppingTarget)
from cellar.models.base import Program
from cellar.models.reference import Variety
from cellar.services import blending, generator, reservations, topping
from cellar.services import partx


def _placement(lot, container_id, gal, filled_at):
    c = Container.objects.create(container_id=container_id, type=Container.Type.BARREL,
                                 capacity_gal=60, active=True)
    return AgingPlacement.objects.create(lot=lot, container=c, volume_gal=Decimal(gal),
                                         filled_at=filled_at)


class PartialFillTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.table = Variety.objects.create(name="Verdelho", notes="")
        self.port_v = Variety.objects.create(name="Touriga Nacional", notes="")
        self.target = generator.create_lot(2025, self.port_v, Program.PORT)
        self.source = generator.create_lot(2025, self.table, Program.TABLE)
        # Tax class comes from the booking, not Lot.program — the port lot must
        # actually be booked in class b for this to be a cross-class fill.
        BookToBond.objects.create(lot=self.target, booked_at=self.today, notes="",
                                  gallons_produced=500, tax_class="b")
        BookToBond.objects.create(lot=self.source, booked_at=self.today, notes="",
                                  gallons_produced=500, tax_class="a")
        self.p = _placement(self.target, "B-100", 25, self.today)

    def _top(self, kind, gal):
        ev = ToppingEvent.objects.create(source_lot=self.source, kind=kind,
                                         topped_at=self.today)
        return ToppingTarget.objects.create(event=ev, placement=self.p,
                                            volume_added=Decimal(gal))

    def test_partial_fill_writes_distinct_lineage_type(self):
        t = self._top(ToppingEvent.Kind.PARTIAL_FILL, 35)
        self.assertEqual(t.contribution.relationship_type,
                         LotLineage.Relationship.PARTIAL_FILL)

    def test_routine_topping_still_writes_topping_type(self):
        t = self._top(ToppingEvent.Kind.ROUTINE, 2)
        self.assertEqual(t.contribution.relationship_type,
                         LotLineage.Relationship.TOPPING)

    def test_partial_fill_counts_toward_composition_but_not_the_flag(self):
        self._top(ToppingEvent.Kind.PARTIAL_FILL, 35)
        p = AgingPlacement.objects.get(pk=self.p.pk)
        # real wine, so it counts as foreign volume...
        self.assertEqual(p.foreign_topped_gal, Decimal("35.0"))
        self.assertEqual(p.partial_fill_gal, Decimal("35.0"))
        # ...but the 5-gal flag watches routine creep, which this is not
        self.assertEqual(p.routine_topped_gal, Decimal("0"))
        self.assertFalse(p.is_flagged)
        self.assertTrue(p.has_partial_fill)

    def test_routine_creep_still_trips_the_flag_after_a_partial_fill(self):
        """The separate accumulator must not let a big fill mask later creep."""
        self._top(ToppingEvent.Kind.PARTIAL_FILL, 35)
        self._top(ToppingEvent.Kind.ROUTINE, 6)
        p = AgingPlacement.objects.get(pk=self.p.pk)
        self.assertTrue(p.is_flagged)

    def test_partial_fill_across_tax_class_reports_in_part_x(self):
        """The gap this type exists to close: a 35-gal table-into-port fill is a
        material cross-class movement and belongs in footnote 5/."""
        self._top(ToppingEvent.Kind.PARTIAL_FILL, 35)
        out = partx.build_part_x(self.today.year, self.today.month)
        kinds = [e["trigger"] for e in out["entries"]]
        self.assertIn("change_of_tax_class", kinds)

    def test_routine_topping_across_tax_class_stays_out_of_part_x(self):
        """De minimis: reporting every gallon of evaporation replacement would
        bury the report."""
        self._top(ToppingEvent.Kind.ROUTINE, 2)
        out = partx.build_part_x(self.today.year, self.today.month)
        self.assertNotIn("change_of_tax_class", [e["trigger"] for e in out["entries"]])


class BarrelLevelBlendTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        v = Variety.objects.create(name="Zinfandel", notes="")
        self.src = generator.create_lot(2025, v, Program.TABLE)
        self.dest = generator.create_lot(2025, v, Program.TABLE)
        BookToBond.objects.create(lot=self.src, booked_at=self.today, notes="",
                                  gallons_produced=120)
        BookToBond.objects.create(lot=self.dest, booked_at=self.today, notes="",
                                  gallons_produced=100)
        self.a = _placement(self.src, "B-201", 60, self.today)
        self.b = _placement(self.src, "B-202", 60, self.today)

    def test_blend_without_sources_is_unchanged(self):
        edge = blending.blend(self.src, self.dest, blended_at=self.today,
                              kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=50)
        self.assertEqual(LineageSource.objects.filter(lineage=edge).count(), 0)

    def test_blend_records_contributing_barrels(self):
        edge = blending.blend(
            self.src, self.dest, blended_at=self.today,
            kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=90,
            sources=[(self.a, 60), (self.b, 30)])
        rows = LineageSource.objects.filter(lineage=edge)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(sum(r.volume_gal for r in rows), Decimal("90.0"))

    def test_detail_must_sum_to_the_edge(self):
        with self.assertRaises(ValueError):
            blending.blend(self.src, self.dest, blended_at=self.today,
                           kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=90,
                           sources=[(self.a, 60), (self.b, 10)])

    def test_barrel_must_hold_the_source_lot(self):
        stray = _placement(self.dest, "B-999", 60, self.today)
        with self.assertRaises(ValueError):
            blending.blend(self.src, self.dest, blended_at=self.today,
                           kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=60,
                           sources=[(stray, 60)])


class ReservationTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        v = Variety.objects.create(name="Barbera", notes="")
        self.src = generator.create_lot(2025, v, Program.TABLE)
        self.dest = generator.create_lot(2025, v, Program.TABLE)
        BookToBond.objects.create(lot=self.src, booked_at=self.today, notes="",
                                  gallons_produced=120)
        BookToBond.objects.create(lot=self.dest, booked_at=self.today, notes="",
                                  gallons_produced=100)
        self.a = _placement(self.src, "B-301", 60, self.today)
        self.b = _placement(self.src, "B-302", 60, self.today)

    def test_whole_lot_reservation(self):
        res, warnings = reservations.reserve(
            self.src, reserved_at=self.today, label="Naked Wines",
            purpose=LotReservation.Purpose.SALE)
        self.assertTrue(res.is_open)
        self.assertIsNone(res.placement)
        self.assertEqual(warnings, [])

    def test_double_booking_warns_but_writes(self):
        reservations.reserve(self.src, reserved_at=self.today, placement=self.a,
                             label="Buyer A")
        res, warnings = reservations.reserve(self.src, reserved_at=self.today,
                                             placement=self.a, label="Buyer B")
        self.assertTrue(res.is_open)                 # written anyway
        self.assertTrue(any("already reserved" in w for w in warnings))

    def test_blend_naming_the_barrel_satisfies_the_reservation(self):
        res, _ = reservations.reserve(self.src, reserved_at=self.today,
                                      placement=self.a, label="Buyer A")
        edge = blending.blend(self.src, self.dest, blended_at=self.today,
                              kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=60,
                              sources=[(self.a, 60)])
        closed = reservations.settle_for_edge(edge)
        res.refresh_from_db()
        self.assertIn(res, closed)
        self.assertFalse(res.is_open)
        self.assertEqual(res.satisfied_at, self.today)

    def test_other_barrels_reservation_survives(self):
        keep, _ = reservations.reserve(self.src, reserved_at=self.today,
                                       placement=self.b, label="Buyer B")
        edge = blending.blend(self.src, self.dest, blended_at=self.today,
                              kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=60,
                              sources=[(self.a, 60)])
        reservations.settle_for_edge(edge)
        keep.refresh_from_db()
        self.assertTrue(keep.is_open)

    def test_partial_blend_without_detail_does_not_guess(self):
        """Silently discarding a real commitment is worse than leaving one open."""
        res, _ = reservations.reserve(self.src, reserved_at=self.today,
                                      placement=self.a, label="Buyer A")
        edge = blending.blend(self.src, self.dest, blended_at=self.today,
                              kind=LotLineage.Relationship.PARTIAL_BLEND, volume_gal=60)
        reservations.settle_for_edge(edge)
        res.refresh_from_db()
        self.assertTrue(res.is_open)
        self.assertIn(res, reservations.unsettled_after_blend(edge))

    def test_release_closes_without_satisfying(self):
        res, _ = reservations.reserve(self.src, reserved_at=self.today,
                                      placement=self.a, label="Buyer A")
        reservations.release(res, released_at=self.today)
        res.refresh_from_db()
        self.assertFalse(res.is_open)
        self.assertIsNone(res.satisfied_at)
