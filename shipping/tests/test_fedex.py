"""FedEx adapter: request shape, response parsing, auth caching, errors."""
import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.test import SimpleTestCase

from shipping.carriers import Address, CarrierConfigError, CarrierError, LabelRequest, Parcel
from shipping.carriers.fedex import FedExAdapter, default_service

from .fakes import PDF_BYTES, FakeResponse, fedex_session, ship_error, ship_ok


def adapter(session=None, **kw):
    args = dict(environment="sandbox", client_id="cid-abcdef", client_secret="sec",
                account_number="123456789", session=session or fedex_session())
    args.update(kw)
    return FedExAdapter(**args)


def request(**kw):
    args = dict(
        ship_from=Address(name="Shipping", company="St. Amant Winery", street1="1 Winemaster Way",
                          city="Lodi", state="CA", postal_code="95240", phone="(209) 555-0100"),
        ship_to=Address(name="Pat Buyer", street1="12 Elm St", street2="Apt 3", city="Austin",
                        state="TX", postal_code="78701", phone="512-555-0123", residential=True),
        parcel=Parcel(Decimal("15.5"), Decimal("5"), Decimal("5.5"), Decimal("3.6"),
                      reference="C7-10422"),
        service="GROUND_HOME_DELIVERY", ship_date=dt.date(2026, 10, 22))
    args.update(kw)
    return LabelRequest(**args)


class PayloadTests(SimpleTestCase):
    def setUp(self):
        self.payload = adapter().build_ship_payload(request())
        self.rs = self.payload["requestedShipment"]
        self.pkg = self.rs["requestedPackageLineItems"][0]

    def test_alcohol_consumer_and_adult_signature_at_package_level(self):
        special = self.pkg["packageSpecialServices"]
        self.assertEqual(special["specialServiceTypes"], ["ALCOHOL", "SIGNATURE_OPTION"])
        self.assertEqual(special["alcoholDetail"], {"alcoholRecipientType": "CONSUMER"})
        self.assertEqual(special["signatureOptionType"], "ADULT")
        self.assertNotIn("shipmentSpecialServices", self.rs)

    def test_shipper_agreement_only_sent_when_configured(self):
        p = adapter(alcohol_shipper_agreement="WINERY").build_ship_payload(request())
        detail = p["requestedShipment"]["requestedPackageLineItems"][0][
            "packageSpecialServices"]["alcoholDetail"]
        self.assertEqual(detail["shipperAgreementType"], "WINERY")

    def test_4x6_pdf_label(self):
        self.assertEqual(self.rs["labelSpecification"],
                         {"imageType": "PDF", "labelStockType": "STOCK_4X6"})
        self.assertEqual(self.payload["labelResponseOptions"], "LABEL")

    def test_dimensions_round_up_to_whole_inches(self):
        self.assertEqual(self.pkg["dimensions"],
                         {"length": 16, "width": 5, "height": 6, "units": "IN"})
        self.assertEqual(self.pkg["weight"], {"units": "LB", "value": 3.6})

    def test_parties(self):
        to = self.rs["recipients"][0]
        self.assertEqual(to["address"]["streetLines"], ["12 Elm St", "Apt 3"])
        self.assertTrue(to["address"]["residential"])
        self.assertEqual(to["contact"]["phoneNumber"], "5125550123")
        self.assertEqual(self.rs["shipper"]["contact"]["phoneNumber"], "2095550100")
        self.assertNotIn("residential", self.rs["shipper"]["address"])
        self.assertEqual(self.payload["accountNumber"], {"value": "123456789"})

    def test_reference_printed_on_label(self):
        self.assertEqual(self.pkg["customerReferences"][0]["value"], "C7-10422")

    def test_no_alcohol_no_special_services(self):
        p = adapter().build_ship_payload(request(contains_alcohol=False, adult_signature=False))
        self.assertNotIn("packageSpecialServices",
                         p["requestedShipment"]["requestedPackageLineItems"][0])

    def test_default_service(self):
        self.assertEqual(default_service(True), "GROUND_HOME_DELIVERY")
        self.assertEqual(default_service(False), "FEDEX_GROUND")
        self.assertEqual(default_service(None), "FEDEX_GROUND")


class CreateLabelTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_parses_tracking_charge_and_document(self):
        result = adapter().create_label(request())
        self.assertEqual(result.tracking_number, "794600000001")
        self.assertEqual(result.net_charge, Decimal("13.87"))
        self.assertEqual(result.currency, "USD")
        self.assertEqual(len(result.documents), 1)
        self.assertEqual(result.documents[0].content, PDF_BYTES)
        self.assertEqual(result.environment, "sandbox")

    def test_keeps_every_document(self):
        s = fedex_session({("POST", "/ship/v1/shipments"): lambda: ship_ok(docs=2)})
        self.assertEqual(len(adapter(s).create_label(request()).documents), 2)

    def test_images_stripped_from_stored_summary(self):
        result = adapter().create_label(request())
        doc = result.response_summary["output"]["transactionShipments"][0][
            "pieceResponses"][0]["packageDocuments"][0]
        self.assertIn("stripped", doc["encodedLabel"])

    def test_carrier_error_raised_with_code(self):
        s = fedex_session({("POST", "/ship/v1/shipments"): ship_error})
        with self.assertRaises(CarrierError) as ctx:
            adapter(s).create_label(request())
        self.assertEqual(ctx.exception.code, "SHIPMENT.ALCOHOL.NOTALLOWED")
        self.assertEqual(ctx.exception.status, 400)

    def test_label_without_image_is_an_error_naming_the_tracking_number(self):
        body = ship_ok()._body
        body["output"]["transactionShipments"][0]["pieceResponses"][0]["packageDocuments"] = []
        s = fedex_session({("POST", "/ship/v1/shipments"): FakeResponse(200, body)})
        with self.assertRaisesRegex(CarrierError, "794600000001"):
            adapter(s).create_label(request())

    def test_token_cached_between_calls(self):
        s = fedex_session()
        a = adapter(s)
        a.create_label(request())
        a.create_label(request())
        self.assertEqual(sum(1 for c in s.calls if c[1].endswith("/oauth/token")), 1)
        ship_call = next(c for c in s.calls if c[1].endswith("/ship/v1/shipments"))
        self.assertEqual(ship_call[2]["headers"]["Authorization"], "Bearer tok-123")

    def test_bad_credentials_are_a_config_error(self):
        s = fedex_session({("POST", "/oauth/token"): FakeResponse(
            401, {"errors": [{"code": "NOT.AUTHORIZED.ERROR", "message": "bad creds"}]})})
        with self.assertRaises(CarrierConfigError):
            adapter(s).create_label(request())

    def test_non_json_error_body(self):
        s = fedex_session({("POST", "/ship/v1/shipments"): FakeResponse(503, None, "down")})
        with self.assertRaisesRegex(CarrierError, "down"):
            adapter(s).create_label(request())


class ConfigTests(SimpleTestCase):
    def test_missing_credentials_named(self):
        with self.assertRaisesRegex(CarrierConfigError, "FEDEX_CLIENT_SECRET"):
            adapter(client_secret="")

    def test_bad_environment(self):
        with self.assertRaises(CarrierConfigError):
            adapter(environment="prod")

    def test_production_url(self):
        self.assertEqual(adapter(environment="production").base_url, "https://apis.fedex.com")


class VoidTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_void_sends_tracking_and_account(self):
        s = fedex_session()
        adapter(s).void_label("794600000001")
        call = next(c for c in s.calls if c[0] == "PUT")
        self.assertEqual(call[2]["json"]["trackingNumber"], "794600000001")
        self.assertEqual(call[2]["json"]["accountNumber"], {"value": "123456789"})

    def test_void_not_cancelled_raises(self):
        s = fedex_session({("PUT", "/ship/v1/shipments/cancel"): FakeResponse(
            200, {"output": {"cancelledShipment": False}})})
        with self.assertRaises(CarrierError):
            adapter(s).void_label("794600000001")
