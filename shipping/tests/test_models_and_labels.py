"""Seed data, permission groups, append-only records, and the label services."""
import datetime as dt
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from shipping.carriers import Address, CarrierError, LabelRequest, Parcel
from shipping.carriers.fedex import FedExAdapter
from shipping.models import (BottleWeight, BoxSize, Label, LabelDocument, Package)
from shipping.permissions import DEFAULT_GROUPS, ensure_default_groups
from shipping.services.labels import LabelRefused, buy_label, void_label

from .fakes import PDF_BYTES, fedex_session, ship_error


def fedex(session=None, environment="sandbox"):
    return FedExAdapter(environment=environment, client_id="cid-abcdef", client_secret="s",
                        account_number="123456789", session=session or fedex_session())


def label_request(weight="3.6"):
    a = Address(name="A", street1="1 Main", city="Lodi", state="CA", postal_code="95240",
                phone="2095550100")
    return LabelRequest(ship_from=a, ship_to=a,
                        parcel=Parcel(Decimal("15.5"), Decimal("5"), Decimal("5.5"),
                                      Decimal(weight)),
                        service="FEDEX_GROUND", ship_date=dt.date(2026, 10, 22))


class SeedTests(TestCase):
    def test_boxes_seeded_from_nates_table(self):
        self.assertEqual(BoxSize.objects.count(), 8)
        four = BoxSize.objects.get(name="4 Bottle")
        self.assertEqual((four.length_in, four.width_in, four.height_in),
                         (Decimal("20"), Decimal("15"), Decimal("5.5")))
        self.assertEqual((four.tare_lb, four.max_bottles, four.max_weight_lb),
                         (Decimal("1.9"), 4, Decimal("17")))
        self.assertEqual(BoxSize.objects.get(name="Port").use, BoxSize.Use.PORT_CLUB)
        self.assertEqual(BoxSize.objects.get(name="Magnum").use, BoxSize.Use.MAGNUM)

    def test_bottle_weights(self):
        self.assertEqual({b.ml: b.weight_lb for b in BottleWeight.objects.all()},
                         {500: Decimal("2.16"), 750: Decimal("3.00"), 1500: Decimal("7.50")})
        self.assertTrue(BottleWeight.objects.get(ml=1500).is_magnum)

    def test_declared_weight_is_tare_plus_bottles(self):
        port = BoxSize.objects.get(name="Port")
        self.assertEqual(port.declared_weight([Decimal("2.16")]), Decimal("4.16"))
        twelve = BoxSize.objects.get(name="12 Bottle")
        self.assertEqual(twelve.declared_weight([Decimal("3.0")] * 12), Decimal("40.9"))

    def test_declared_weight_capped_at_max(self):
        one = BoxSize.objects.get(name="1 Bottle")
        self.assertEqual(one.declared_weight([Decimal("7.5")]), Decimal("4"))


class GroupTests(TestCase):
    def test_default_groups_have_their_permissions(self):
        packer = Group.objects.get(name="Packer")
        self.assertEqual(set(packer.permissions.values_list("codename", flat=True)),
                         set(DEFAULT_GROUPS["Packer"]))
        admin = Group.objects.get(name="Shipping Admin")
        self.assertIn("void_labels", admin.permissions.values_list("codename", flat=True))

    def test_existing_group_is_never_reset(self):
        packer = Group.objects.get(name="Packer")
        packer.permissions.clear()
        ensure_default_groups()
        self.assertEqual(packer.permissions.count(), 0)


class BuyAndVoidTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_buy_saves_label_and_documents(self):
        label = buy_label(label_request(), adapter=fedex())
        self.assertEqual(label.tracking_number, "794600000001")
        self.assertEqual(label.environment, "sandbox")
        self.assertEqual(label.net_charge, Decimal("13.87"))
        self.assertEqual(bytes(label.documents.get().content), PDF_BYTES)

    def test_carrier_refusal_writes_nothing(self):
        s = fedex_session({("POST", "/ship/v1/shipments"): ship_error})
        with self.assertRaises(CarrierError):
            buy_label(label_request(), adapter=fedex(s))
        self.assertFalse(Label.objects.exists())

    def test_one_live_label_per_package(self):
        pkg = Package.objects.create(box=BoxSize.objects.get(name="1 Bottle"),
                                     bottle_count=1, weight_lb=Decimal("3.6"))
        buy_label(label_request(), package=pkg, adapter=fedex())
        with self.assertRaises(LabelRefused):
            buy_label(label_request(), package=pkg, adapter=fedex())

    def test_void_then_rebuy(self):
        pkg = Package.objects.create(box=BoxSize.objects.get(name="1 Bottle"),
                                     bottle_count=1, weight_lb=Decimal("3.6"))
        first = buy_label(label_request(), package=pkg, adapter=fedex())
        void_label(first, adapter=fedex())
        first.refresh_from_db()
        self.assertIsNotNone(first.voided_at)
        buy_label(label_request(), package=pkg, adapter=fedex())   # allowed now
        self.assertEqual(pkg.labels.filter(voided_at__isnull=True).count(), 1)

    def test_void_refused_by_carrier_leaves_label_live(self):
        from .fakes import FakeResponse
        label = buy_label(label_request(), adapter=fedex())
        s = fedex_session({("PUT", "/ship/v1/shipments/cancel"): FakeResponse(
            200, {"output": {"cancelledShipment": False}})})
        with self.assertRaises(CarrierError):
            void_label(label, adapter=fedex(s))
        label.refresh_from_db()
        self.assertIsNone(label.voided_at)

    def test_void_refuses_environment_mismatch(self):
        label = buy_label(label_request(), adapter=fedex())
        with self.assertRaises(LabelRefused):
            void_label(label, adapter=fedex(environment="production"))

    # Pinned off: Nate's local .env turns ALLOW_HARD_DELETE on for the testing
    # phase, and this test must check the production guard, not the escape hatch.
    @override_settings(ALLOW_HARD_DELETE=False)
    def test_label_is_append_only(self):
        label = buy_label(label_request(), adapter=fedex())
        label.weight_lb = Decimal("9")
        with self.assertRaises(ValueError):
            label.save()
        with self.assertRaises(ValueError):
            label.delete()
        doc = LabelDocument.objects.get(label=label)
        with self.assertRaises(ValueError):
            doc.save()


SANDBOX = {"FEDEX": {"ENV": "sandbox", "CLIENT_ID": "cid-abcdef", "CLIENT_SECRET": "s",
                     "ACCOUNT_NUMBER": "123456789"},
           "UPS": {}, "C7": {},
           "SHIP_FROM": {"company": "St. Amant Winery", "name": "Shipping",
                         "street1": "1 Winemaster Way", "street2": "", "city": "Lodi",
                         "state": "CA", "postal_code": "95240", "country": "US",
                         "phone": "2095550100", "email": ""}}


class TestLabelCommandTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, *args, settings_=SANDBOX):
        out = StringIO()
        self.adapter = fedex()
        with override_settings(SHIPPING=settings_), \
                mock.patch("shipping.management.commands.fedex_test_label.get_adapter",
                           return_value=self.adapter):
            call_command("fedex_test_label", "--out", self.tmp.name, *args, stdout=out)
        return out.getvalue()

    def test_writes_pdf_and_records_label(self):
        out = self.run_cmd("--box", "4 Bottle", "--bottles", "4")
        self.assertIn("alcohol CONSUMER + adult signature", out)
        self.assertIn("13.90 lb", out)         # 1.9 tare + 4 × 3.0
        pdfs = list(Path(self.tmp.name).glob("*.pdf"))
        self.assertEqual(len(pdfs), 1)
        self.assertEqual(pdfs[0].read_bytes(), PDF_BYTES)
        self.assertEqual(Label.objects.get().weight_lb, Decimal("13.9"))

    def test_void_flag(self):
        self.run_cmd()
        tracking = Label.objects.get().tracking_number
        self.assertIn("Voided", self.run_cmd("--void", tracking))
        self.assertIsNotNone(Label.objects.get().voided_at)

    def test_refuses_production(self):
        prod = {**SANDBOX, "FEDEX": {**SANDBOX["FEDEX"], "ENV": "production"}}
        with self.assertRaisesRegex(CommandError, "sandbox"):
            self.run_cmd(settings_=prod)

    def test_requires_ship_from_phone(self):
        nophone = {**SANDBOX, "SHIP_FROM": {**SANDBOX["SHIP_FROM"], "phone": ""}}
        with self.assertRaisesRegex(CommandError, "SHIP_FROM_PHONE"):
            self.run_cmd(settings_=nophone)

    def test_bad_credentials_named_as_such(self):
        from .fakes import FakeResponse
        bad = fedex(fedex_session({("POST", "/oauth/token"): FakeResponse(
            401, {"errors": [{"code": "NOT.AUTHORIZED.ERROR", "message": "bad"}]})}))
        with override_settings(SHIPPING=SANDBOX), \
                mock.patch("shipping.management.commands.fedex_test_label.get_adapter",
                           return_value=bad), \
                self.assertRaisesRegex(CommandError, "rejected the API credentials"):
            call_command("fedex_test_label", "--out", self.tmp.name, stdout=StringIO())

    def test_service_override(self):
        self.run_cmd("--service", "FEDEX_2_DAY")
        self.assertEqual(Label.objects.get().service, "FEDEX_GROUND")   # fake echoes Ground
        # The request itself must carry the forced service:
        sent = self.adapter.session.calls[-1][2]["json"]["requestedShipment"]["serviceType"]
        self.assertEqual(sent, "FEDEX_2_DAY")

    def test_no_alcohol_flag(self):
        out = self.run_cmd("--no-alcohol")
        self.assertIn("NOT a certification label", out)
        sent = self.adapter.session.calls[-1][2]["json"]["requestedShipment"][
            "requestedPackageLineItems"][0]["packageSpecialServices"]
        self.assertEqual(sent["specialServiceTypes"], ["SIGNATURE_OPTION"])
        self.assertNotIn("alcoholDetail", sent)

    def test_too_many_bottles(self):
        with self.assertRaisesRegex(CommandError, "holds 2"):
            self.run_cmd("--box", "2 Bottle", "--bottles", "3")
