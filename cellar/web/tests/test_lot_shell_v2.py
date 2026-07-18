"""
Regression smoke test for the lot dashboard v2 (full-page-per-tile shell).

Covers the two things a delivery must prove: the data/context layer builds, and
every page renders via the Django test client. Exercises both summary-card
variants (fermentation vs aging), the shared Gantt (empty + with markers), the
new Compliance read-view, and confirms the legacy lot page is untouched.

SECURE_SSL_REDIRECT is pinned off so the test client's http requests aren't
301'd in the non-DEBUG test settings.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from cellar.models.base import Program
from cellar.models.reference import Variety
from cellar.models import Reading, BookToBond
from cellar.services import generator

TILES = ["fermentation", "additions", "movement", "oak",
         "composition", "compliance", "cost", "labs"]


@override_settings(
    SECURE_SSL_REDIRECT=False,
    # Plain static storage so the test doesn't require a collectstatic manifest
    # (prod uses whitenoise's CompressedManifestStaticFilesStorage).
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class LotDashboardV2Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        U = get_user_model()
        cls.user = U.objects.create_user("tester", password="pw",
                                         is_staff=True, is_superuser=True)
        cls.variety = Variety.objects.create(name="Verdelho", notes="")
        # A fresh receiving lot → fermentation-variant card.
        cls.lot = generator.create_lot(2025, cls.variety, Program.TABLE)
        # A couple of Brix readings so the Gantt and progress have data.
        now = timezone.now()
        for i, brix in enumerate((22.0, 18.5)):
            Reading.objects.create(
                lot=cls.lot, analyte=Reading.Analyte.BRIX, value=brix,
                method="hydrometer", measured_at=now - timedelta(days=6 - i * 2),
                notes="")

    def setUp(self):
        self.c = Client()
        self.c.login(username="tester", password="pw")

    # ---- every tile renders (fermentation variant) ------------------------
    def test_all_tiles_render_200(self):
        for t in TILES:
            with self.subTest(tile=t):
                r = self.c.get(f"/lots/{self.lot.pk}/d/{t}/")
                self.assertEqual(r.status_code, 200, f"{t} did not 200")
                html = r.content.decode()
                # shell chrome present on every tile
                self.assertIn("tile-grid", html)
                self.assertIn(self.lot.code, html)

    def test_read_tiles_render_body_server_side(self):
        # composition/compliance/cost/labs render their body inline (no htmx div)
        r = self.c.get(f"/lots/{self.lot.pk}/d/compliance/")
        self.assertContains(r, "Compliance ledger")
        r = self.c.get(f"/lots/{self.lot.pk}/d/cost/")
        self.assertEqual(r.status_code, 200)
        r = self.c.get(f"/lots/{self.lot.pk}/d/labs/")
        self.assertEqual(r.status_code, 200)

    def test_capture_tiles_lazyload_existing_fragment(self):
        # capture tiles are full pages whose body lazy-loads the existing fragment
        r = self.c.get(f"/lots/{self.lot.pk}/d/fermentation/")
        self.assertContains(r, 'id="lot-panel"')
        self.assertContains(r, f"/lots/{self.lot.pk}/ferment/")

    def test_fermentation_variant_card(self):
        r = self.c.get(f"/lots/{self.lot.pk}/d/composition/")
        html = r.content.decode()
        # pre-bond → no "in bond" status pill text; sugar-depletion is possible
        self.assertNotIn("· in bond", html)

    def test_gantt_present_with_markers(self):
        r = self.c.get(f"/lots/{self.lot.pk}/d/fermentation/")
        html = r.content.decode()
        self.assertIn("gantt-svg", html)
        self.assertIn("gantt-dot", html)  # the two readings

    def test_cost_and_labs_hide_lifecycle_gantt(self):
        for t in ("cost", "labs"):
            r = self.c.get(f"/lots/{self.lot.pk}/d/{t}/")
            self.assertNotIn("gantt-svg", r.content.decode(),
                             f"{t} should not show the lifecycle gantt")

    # ---- aging variant (in bond) ------------------------------------------
    def test_in_bond_switches_to_aging_variant(self):
        BookToBond.objects.create(
            lot=self.lot, booked_at=timezone.localdate(), notes="",
            gallons_produced=1000)
        r = self.c.get(f"/lots/{self.lot.pk}/d/compliance/")
        html = r.content.decode()
        self.assertIn("in bond", html.lower())
        self.assertContains(r, "Booked to bond")   # ledger row from the decomposition
        # aging card headline
        r2 = self.c.get(f"/lots/{self.lot.pk}/d/oak/")
        self.assertIn("Last topped", r2.content.decode())

    # ---- isolation: legacy page untouched ---------------------------------
    def test_legacy_lot_detail_still_renders(self):
        r = self.c.get(f"/lots/{self.lot.pk}/")
        self.assertEqual(r.status_code, 200)
        # legacy tab bar still there
        self.assertIn("lot-panel", r.content.decode())

    # ---- capture-tile action switcher (folded satellite tabs) -------------
    def test_additions_folds_sweeten_and_hides_fortify_for_table_lot(self):
        r = self.c.get(f"/lots/{self.lot.pk}/d/additions/")
        html = r.content.decode()
        self.assertIn("section-switch", html)
        self.assertIn("Backsweeten", html)          # sweeten folded in
        self.assertNotIn("Re-fortification", html)   # table lot → no fortify action

    def test_additions_action_switches_body_fragment(self):
        r = self.c.get(f"/lots/{self.lot.pk}/d/additions/?action=sweeten")
        # body now lazy-loads the sweeten fragment, not the additions one
        self.assertContains(r, f"/lots/{self.lot.pk}/sweeten/")

    def test_port_lot_shows_refortification_action(self):
        port_lot = generator.create_lot(2025, self.variety, Program.PORT)
        r = self.c.get(f"/lots/{port_lot.pk}/d/additions/")
        self.assertContains(r, "Re-fortification")

    def test_movement_hides_bottling_until_bondable(self):
        # fresh receiving lot can't be bottled yet → no bottling action
        r = self.c.get(f"/lots/{self.lot.pk}/d/movement/")
        self.assertNotIn("Bottling", r.content.decode())

    def test_fermentation_book_to_bond_action_appears_when_in_bond(self):
        BookToBond.objects.create(lot=self.lot, booked_at=timezone.localdate(),
                                  notes="", gallons_produced=1000)
        r = self.c.get(f"/lots/{self.lot.pk}/d/fermentation/")
        self.assertContains(r, "Book to bond")
