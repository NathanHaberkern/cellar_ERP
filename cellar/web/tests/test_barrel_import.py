"""
Regression test for the barrel/rack seed importer and the aging model changes
(Container.pool, Container SS-drum class, Rack.size_class, AgingPlacement legacy
occupancy + explicit fill_number).

Builds a tiny in-memory workbook covering the tricky cases — a large-rack 130,
a 55-gal SS drum, an EMPTY barrel, a legacy-filled barrel with prior_fills, and
a rack whose two rows disagree on location — and asserts the fleet lands right.
"""
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from cellar.models import Container, Rack, Location, RackAssignment, AgingPlacement
from cellar.models.aging import OakTier

HEADERS = ["barrel_id", "size_gal", "pool", "rack_id", "rack_position",
           "column", "room", "current_lot", "fill_date", "prior_fills", "barrel_barcode"]

ROWS = [
    # a full standard rack, both filled, one with prior fills (2nd use)
    [2501, 60, "Table", "R-001", 1, 3, "New Barrel Room", "25VERD", "2025-11-05", 1, None],
    [2502, 70, "table", "R-001", 2, 3, "New Barrel Room", "25VERD", "2025-11-05", 0, None],
    # a large rack of 130s
    [3401, 130, "Port", "R-200", 1, 6, "Old Barrel Room", "20PORT", "2020-05-01", 2, None],
    [3402, 130, "Port", "R-200", 2, 6, "Old Barrel Room", "20PORT", "2020-05-01", None, None],
    # a 55-gal SS drum, empty, no column (Bldg H)
    [9001, 55, "Port", None, None, None, "Bldg. H", "EMPTY", None, None, None],
    # an empty standard barrel (feeds the empty pool)
    [2601, 60, "Table", "R-050", 1, 2, "New Barrel Room", "EMPTY", None, None, None],
    # a rack whose two rows disagree on column → conflict, resolve from pos 1
    [7001, 60, "Table", "R-900", 1, 1, "Old Barrel Room", "EMPTY", None, None, None],
    [7002, 60, "Table", "R-900", 2, 4, "Old Barrel Room", "EMPTY", None, None, None],
]


def _write_workbook(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for r in ROWS:
        ws.append(r)
    wb.save(path)


class BarrelSeedImportTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp()
        cls.xlsx = str(Path(cls.tmp) / "seed.xlsx")
        _write_workbook(cls.xlsx)

    def _import(self, **kw):
        call_command("import_barrel_seed", self.xlsx, verbosity=0, **kw)

    def test_fleet_counts(self):
        self._import()
        self.assertEqual(Container.objects.count(), 8)
        self.assertEqual(Rack.objects.count(), 4)          # R-001, R-200, R-050, R-900
        self.assertEqual(AgingPlacement.objects.filter(lot__isnull=True).count(), 4)  # 4 filled

    def test_ss_drum_class_and_pool(self):
        self._import()
        drum = Container.objects.get(container_id="9001")
        self.assertEqual(drum.type, Container.Type.SS_DRUM)
        self.assertFalse(drum.is_oak)
        self.assertEqual(drum.capacity_gal, 55)
        self.assertEqual(drum.pool, "port")
        # pool normalised case-insensitively
        self.assertEqual(Container.objects.get(container_id="2502").pool, "table")

    def test_rack_size_class(self):
        self._import()
        self.assertEqual(Rack.objects.get(rack_id="R-200").size_class, "large")
        self.assertEqual(Rack.objects.get(rack_id="R-001").size_class, "standard")

    def test_legacy_occupancy_and_explicit_fill_number(self):
        self._import()
        # prior_fills=1 → fill_number 2 → first_use; prior_fills=0 → fill_number 1 → new
        p1 = AgingPlacement.objects.get(container__container_id="2501")
        self.assertEqual(p1.legacy_lot_code, "25VERD")
        self.assertIsNone(p1.lot_id)
        self.assertEqual(p1.fill_number, 2)
        self.assertEqual(p1.oak_tier, OakTier.FIRST)
        p2 = AgingPlacement.objects.get(container__container_id="2502")
        self.assertEqual(p2.fill_number, 1)
        self.assertEqual(p2.oak_tier, OakTier.NEW)
        # per-barrel fill volume = capacity − 3 headspace (60→57, 70→67)
        self.assertEqual(p1.volume_gal, 57)
        self.assertEqual(p2.volume_gal, 67)

    def test_empty_barrels_have_no_placement(self):
        self._import()
        empty = Container.objects.get(container_id="2601")
        self.assertIsNone(empty.current_placement())
        self.assertIsNone(empty.current_occupant_label())

    def test_location_conflict_resolves_from_position_one(self):
        self._import()
        # R-900: pos1 col 1, pos2 col 4 → keep col 1 (Old Barrel Room → OBC1)
        rack = Rack.objects.get(rack_id="R-900")
        self.assertEqual(rack.location.code, "OBC1")

    def test_bldg_h_no_column_is_room_only(self):
        self._import()
        drum = Container.objects.get(container_id="9001")
        # no rack (no rack_id) → no effective location, but the drum still imported
        self.assertIsNone(drum.current_rack_assignment())

    def test_idempotent_reimport(self):
        self._import()
        self._import()  # second run must not duplicate
        self.assertEqual(Container.objects.count(), 8)
        self.assertEqual(RackAssignment.objects.filter(removed_at__isnull=True).count(), 7)
        self.assertEqual(AgingPlacement.objects.filter(lot__isnull=True).count(), 4)

    def test_wipe_reloads_clean(self):
        self._import()
        self._import(wipe=True)
        self.assertEqual(Container.objects.count(), 8)
