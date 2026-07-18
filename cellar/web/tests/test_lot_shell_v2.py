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
        self.assertIn("gantt-track", html)
        self.assertIn("gdot", html)  # the two readings

    def test_gantt_phase_bands_derive_from_events(self):
        from datetime import timedelta
        from cellar.models import InoculationEvent, PressingEvent, BookToBond, AgingPlacement, Container
        now = timezone.now()
        today = timezone.localdate()
        InoculationEvent.objects.create(lot=self.lot, inoculated_at=now - timedelta(days=60),
                                        yeast_strain="D254", notes="")
        PressingEvent.objects.create(lot=self.lot, pressed_at=now - timedelta(days=45),
                                     disposition="press_fraction", notes="")
        BookToBond.objects.create(lot=self.lot, booked_at=today - timedelta(days=40),
                                  notes="", gallons_produced=1000)
        cont = Container.objects.create(container_id="9100", type=Container.Type.BARREL,
                                        capacity_gal=60, pool="table")
        AgingPlacement.objects.create(lot=self.lot, container=cont,
                                      filled_at=today - timedelta(days=38), volume_gal=57,
                                      fill_number=1)
        r = self.c.get(f"/lots/{self.lot.pk}/d/oak/")
        html = r.content.decode()
        self.assertIn("gphase-primary", html)   # inoc → press
        self.assertIn("gphase-elevage", html)   # barrel-down → now
        self.assertIn("Primary ferment", html)
        # Fruit prep (no harvest) and Finishing (no bottling) stay absent
        self.assertNotIn("gphase-finishing", html)

    def test_cost_and_labs_hide_lifecycle_gantt(self):
        for t in ("cost", "labs"):
            r = self.c.get(f"/lots/{self.lot.pk}/d/{t}/")
            self.assertNotIn("gantt-track", r.content.decode(),
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

    # ---- v2 flip: lot-detail lands on the mode-appropriate tile -----------
    def test_lot_detail_redirects_to_fermentation_pre_bond(self):
        r = self.c.get(f"/lots/{self.lot.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"/lots/{self.lot.pk}/d/fermentation/", r.headers["Location"])

    def test_lot_detail_redirects_to_oak_in_bond(self):
        BookToBond.objects.create(lot=self.lot, booked_at=timezone.localdate(),
                                  notes="", gallons_produced=1000)
        r = self.c.get(f"/lots/{self.lot.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"/lots/{self.lot.pk}/d/oak/", r.headers["Location"])

    def test_legacy_lot_detail_retired(self):
        # the legacy single-page view + route are gone
        r = self.c.get(f"/lots/{self.lot.pk}/legacy/")
        self.assertEqual(r.status_code, 404)

    # ---- dated compliance ledger ------------------------------------------
    def test_compliance_ledger_has_dated_booked_row_and_reconciles(self):
        from cellar.services import compliance_ledger as cl
        BookToBond.objects.create(lot=self.lot, booked_at=timezone.localdate(),
                                  notes="", gallons_produced=1000)
        data = cl.rows(self.lot)
        self.assertTrue(data["reconciles"])
        self.assertTrue(any(r["label"] == "Booked to bond" and r["date"] is not None
                            for r in data["rows"]))
        # rendered page shows the Date column + the booked row
        r = self.c.get(f"/lots/{self.lot.pk}/d/compliance/")
        self.assertContains(r, "Booked to bond")
        self.assertContains(r, "<th>Date</th>", html=False)

    def test_compliance_ledger_reconciles_with_mixed_events(self):
        from datetime import timedelta
        from cellar.services import compliance_ledger as cl
        from cellar.services import volumes
        from cellar.models import VolumeLoss, BulkTaxPaidRemoval
        today = timezone.localdate()
        BookToBond.objects.create(lot=self.lot, booked_at=today - timedelta(days=30),
                                  notes="", gallons_produced=1000)
        VolumeLoss.objects.create(lot=self.lot, volume_gal=12.5,
                                  occurred_at=today - timedelta(days=10),
                                  reason="angel's share", notes="")
        BulkTaxPaidRemoval.objects.create(lot=self.lot, wine_gallons=200,
                                          removed_at=today - timedelta(days=5), notes="")
        data = cl.rows(self.lot)
        self.assertTrue(data["reconciles"])
        self.assertEqual(data["rows"][-1]["balance"], volumes.lot_balance(self.lot))
        # chronological
        dates = [r["date"] for r in data["rows"]]
        self.assertEqual(dates, sorted(dates))

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
