"""
Oak tile v2 — display + two-phase fill flow.

Imports a tiny fleet (mixed 60/70, a port barrel, a spare column), books a lot to
bond, runs the rack-down through the real commit endpoint, and asserts: the empty
picker is pool-filtered, the per-barrel gauge defaults to capacity − headspace,
placements + the barrel-fill total are correct, the racks move to the end column,
and the column → rack → barrel display reflects it.
"""
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from cellar.models.base import Program
from cellar.models.reference import Variety
from cellar.models import BookToBond, Container, Rack, AgingPlacement
from cellar.services import generator, bonding
from cellar.web import oakflow

HEADERS = ["barrel_id", "size_gal", "pool", "rack_id", "rack_position",
           "column", "room", "current_lot", "fill_date", "prior_fills", "barrel_barcode"]
ROWS = [
    [5001, 60, "Table", "R-01", 1, 3, "New Barrel Room", "EMPTY", None, None, None],
    [5002, 70, "Table", "R-01", 2, 3, "New Barrel Room", "EMPTY", None, None, None],
    [5003, 60, "Table", "R-02", 1, 3, "New Barrel Room", "EMPTY", None, None, None],
    [7777, 60, "Table", "R-77", 1, 4, "New Barrel Room", "EMPTY", None, None, None],  # NBC4 target
    [5005, 60, "Port",  "R-09", 1, 6, "Old Barrel Room", "EMPTY", None, None, None],  # wrong pool
]


def _workbook(path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(HEADERS)
    for r in ROWS:
        ws.append(r)
    wb.save(path)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class OakFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp()
        cls.xlsx = str(Path(cls.tmp) / "fleet.xlsx")
        _workbook(cls.xlsx)

    def setUp(self):
        call_command("import_barrel_seed", self.xlsx, verbosity=0)
        U = get_user_model()
        U.objects.create_user("t", password="pw", is_staff=True, is_superuser=True)
        self.variety = Variety.objects.create(name="Verdelho", notes="")
        self.lot = generator.create_lot(2025, self.variety, Program.TABLE)
        BookToBond.objects.create(lot=self.lot, booked_at=timezone.localdate(),
                                  notes="", gallons_produced=200)
        self.c = Client(); self.c.login(username="t", password="pw")

    def test_fill_picker_is_pool_filtered(self):
        r = self.c.get(f"/lots/{self.lot.pk}/oak2/fill/")
        html = r.content.decode()
        self.assertIn("5001", html)          # table empties shown
        self.assertNotIn("5005", html)       # port barrel excluded for a table lot
        self.assertIn("NBC3", html)          # grouped by current column

    def _commit(self, barrels, end_column="NBC4"):
        conts = {b: Container.objects.get(container_id=b) for b in barrels}
        data = {"containers": [str(c.id) for c in conts.values()],
                "end_column": end_column,
                "filled_at": timezone.localdate().isoformat()}
        for c in conts.values():
            data[f"fill_{c.id}"] = str(oakflow._fill_default(c))
        return self.c.post(f"/lots/{self.lot.pk}/oak2/fill/commit/", data)

    def test_fill_creates_placements_with_per_barrel_gauge(self):
        self._commit(["5001", "5002", "5003"])
        ps = AgingPlacement.objects.filter(lot=self.lot)
        self.assertEqual(ps.count(), 3)
        # 60→57, 70→67 (capacity − 3 headspace), per barrel
        self.assertEqual(sorted(float(p.volume_gal) for p in ps), [57.0, 57.0, 67.0])
        self.assertEqual(bonding.barrel_fill_total(self.lot), 181)

    def test_fill_moves_racks_to_end_column(self):
        self._commit(["5001", "5002"], end_column="NBC4")
        self.assertEqual(Rack.objects.get(rack_id="R-01").location.code, "NBC4")

    def test_fill_leave_in_place_keeps_column(self):
        self._commit(["5003"], end_column="__keep__")
        self.assertEqual(Rack.objects.get(rack_id="R-02").location.code, "NBC3")

    def test_display_shows_column_rack_barrel(self):
        self._commit(["5001", "5002"], end_column="NBC4")
        r = self.c.get(f"/lots/{self.lot.pk}/oak2/barrels/")
        html = r.content.decode()
        self.assertIn("NBC4", html)
        self.assertIn("R-01", html)
        self.assertIn("5001", html)
        self.assertIn("New", html)  # tier label

    def test_oak_tile_has_all_actions(self):
        r = self.c.get(f"/lots/{self.lot.pk}/d/oak/")
        html = r.content.decode()
        for label in ("Barrels", "Rack down", "Topping", "Rack-out"):
            self.assertIn(label, html)
