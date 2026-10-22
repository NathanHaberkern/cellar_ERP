"""UPS adapter: request building, response parsing, void, auth, GIF -> 4x6 PDF."""
import base64
import datetime as dt
import io
from decimal import Decimal

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from shipping.carriers import Address, CarrierConfigError, CarrierError, LabelRequest, Parcel
from shipping.carriers.registry import get_adapter
from shipping.carriers.ups import UPSAdapter, gif_to_pdf

from .fakes import FakeResponse, FakeSession


def gif_bytes(w=1400, h=800):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (w, h), 255).save(buf, format="GIF")
    return buf.getvalue()


GIF_B64 = base64.b64encode(gif_bytes()).decode()


def token_ok():
    return FakeResponse(200, {"access_token": "ups-tok", "token_type": "Bearer",
                              "expires_in": "14399", "status": "approved"})


def ship_ok(tracking="1ZTEST0000000001", as_list=False, negotiated=True):
    pkg = {"TrackingNumber": tracking,
           "ShippingLabel": {"ImageFormat": {"Code": "GIF"}, "GraphicImage": GIF_B64}}
    results = {
        "ShipmentIdentificationNumber": tracking,
        "ShipmentCharges": {"TotalCharges": {"CurrencyCode": "USD", "MonetaryValue": "21.40"}},
        "PackageResults": [pkg] if as_list else pkg,
    }
    if negotiated:
        results["NegotiatedRateCharges"] = {
            "TotalCharge": {"CurrencyCode": "USD", "MonetaryValue": "15.10"}}
    return FakeResponse(200, {"ShipmentResponse": {
        "Response": {"ResponseStatus": {"Code": "1", "Description": "Success"}},
        "ShipmentResults": results}})


def void_ok():
    return FakeResponse(200, {"VoidShipmentResponse": {
        "Response": {"ResponseStatus": {"Code": "1"}},
        "SummaryResult": {"Status": {"Code": "1", "Description": "Voided"}}}})


def ups_session(overrides=None):
    responses = {("POST", "/security/v1/oauth/token"): token_ok,
                 ("POST", "/api/shipments/v2409/ship"): ship_ok,
                 ("DELETE", "/void/cancel/1ZTEST0000000001"): void_ok}
    responses.update(overrides or {})
    return FakeSession(responses)


def ups(session=None):
    return UPSAdapter(environment="sandbox", client_id="ups-cid-abcdef", client_secret="s",
                      account_number="A1B2C3", session=session or ups_session())


def req(residential=True, alcohol=True):
    frm = Address(name="Shipping", company="St. Amant Winery", street1="1 Winemaster Way",
                  city="Lodi", state="CA", postal_code="95240", phone="(209) 555-0100")
    to = Address(name="Pat Lee", street1="12 Oak St", street2="Apt 4", city="Denver",
                 state="CO", postal_code="80202", phone="303-555-0100", residential=residential)
    return LabelRequest(ship_from=frm, ship_to=to,
                        parcel=Parcel(Decimal("20"), Decimal("15"), Decimal("5.5"),
                                      Decimal("13.94"), reference="60812/60813"),
                        service="03", ship_date=dt.date(2026, 10, 22), contains_alcohol=alcohol)


class UPSPayloadTests(SimpleTestCase):
    def test_alcohol_at_shipment_level_adult_signature_at_package(self):
        body = ups().build_ship_payload(req())["ShipmentRequest"]
        ship = body["Shipment"]
        self.assertEqual(ship["ShipmentServiceOptions"],
                         {"RestrictedArticles": {"AlcoholicBeveragesIndicator": ""}})
        pkg = ship["Package"][0]
        self.assertEqual(pkg["PackageServiceOptions"], {"DeliveryConfirmation": {"DCISType": "3"}})
        self.assertEqual(pkg["Dimensions"]["Height"], "6")          # 5.5 rounded up
        self.assertEqual(pkg["PackageWeight"]["Weight"], "13.9")
        self.assertEqual(pkg["ReferenceNumber"], [{"Value": "60812/60813"}])
        self.assertEqual(ship["Service"]["Code"], "03")
        self.assertEqual(ship["Shipper"]["ShipperNumber"], "A1B2C3")
        self.assertEqual(ship["PaymentInformation"]["ShipmentCharge"][0]["BillShipper"],
                         {"AccountNumber": "A1B2C3"})
        self.assertEqual(body["LabelSpecification"]["LabelImageFormat"], {"Code": "GIF"})

    def test_residential_flag_and_phone_digits(self):
        ship = ups().build_ship_payload(req())["ShipmentRequest"]["Shipment"]
        self.assertIn("ResidentialAddressIndicator", ship["ShipTo"]["Address"])
        self.assertEqual(ship["ShipTo"]["Address"]["AddressLine"], ["12 Oak St", "Apt 4"])
        self.assertEqual(ship["Shipper"]["Phone"]["Number"], "2095550100")
        self.assertEqual(ship["Shipper"]["Name"], "St. Amant Winery")
        biz = ups().build_ship_payload(req(residential=False))["ShipmentRequest"]["Shipment"]
        self.assertNotIn("ResidentialAddressIndicator", biz["ShipTo"]["Address"])

    def test_no_alcohol_flag_omits_restricted_articles(self):
        ship = ups().build_ship_payload(req(alcohol=False))["ShipmentRequest"]["Shipment"]
        self.assertNotIn("ShipmentServiceOptions", ship)


class UPSCallTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_create_label_parses_tracking_charge_and_pdf(self):
        s = ups_session()
        result = ups(s).create_label(req())
        self.assertEqual(result.tracking_number, "1ZTEST0000000001")
        self.assertEqual(result.master_tracking_number, "1ZTEST0000000001")
        self.assertEqual(result.net_charge, Decimal("15.10"))       # negotiated wins
        self.assertEqual(len(result.documents), 1)
        self.assertTrue(result.documents[0].content.startswith(b"%PDF"))
        self.assertNotIn(GIF_B64, str(result.response_summary))    # images stripped
        token_call = s.calls[0]
        self.assertEqual(token_call[2]["auth"], ("ups-cid-abcdef", "s"))
        self.assertEqual(token_call[2]["headers"]["x-merchant-id"], "A1B2C3")
        ship_call = s.calls[1]
        self.assertEqual(ship_call[2]["headers"]["Authorization"], "Bearer ups-tok")

    def test_package_results_as_list_and_published_rate_fallback(self):
        s = ups_session({("POST", "/api/shipments/v2409/ship"):
                         lambda: ship_ok(as_list=True, negotiated=False)})
        result = ups(s).create_label(req())
        self.assertEqual(result.net_charge, Decimal("21.40"))

    def test_error_body_raises_with_code(self):
        err = FakeResponse(400, {"response": {"errors": [
            {"code": "120900", "message": "User Id and Shipper Number combination is not qualified"}]}})
        s = ups_session({("POST", "/api/shipments/v2409/ship"): err})
        with self.assertRaises(CarrierError) as cm:
            ups(s).create_label(req())
        self.assertEqual(cm.exception.code, "120900")

    def test_bad_credentials_are_config_errors(self):
        s = ups_session({("POST", "/security/v1/oauth/token"): FakeResponse(
            401, {"response": {"errors": [{"code": "10401", "message": "ClientId is Invalid"}]}})})
        with self.assertRaises(CarrierConfigError):
            ups(s).create_label(req())

    def test_void(self):
        s = ups_session()
        ups(s).void_label("1ZTEST0000000001")
        self.assertEqual(s.calls[-1][0], "DELETE")

    def test_void_not_confirmed_raises(self):
        s = ups_session({("DELETE", "/void/cancel/1ZTEST0000000001"): FakeResponse(
            200, {"VoidShipmentResponse": {"SummaryResult": {"Status": {"Code": "0"}}}})})
        with self.assertRaises(CarrierError):
            ups(s).void_label("1ZTEST0000000001")

    def test_missing_credentials(self):
        with self.assertRaises(CarrierConfigError):
            UPSAdapter(environment="sandbox", client_id="", client_secret="",
                       account_number="", session=FakeSession())


class UPSRegistryAndImageTests(SimpleTestCase):
    @override_settings(SHIPPING={"UPS": {"ENV": "sandbox", "CLIENT_ID": "a", "CLIENT_SECRET": "b",
                                         "ACCOUNT_NUMBER": " a1b-2c3 "}})
    def test_registry_builds_ups_and_normalizes_account(self):
        adapter = get_adapter("ups")
        self.assertIsInstance(adapter, UPSAdapter)
        self.assertEqual(adapter.account_number, "A1B2C3")

    def test_gif_becomes_one_portrait_4x6_page(self):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(gif_to_pdf(gif_bytes())))
        self.assertEqual(len(reader.pages), 1)
        box = reader.pages[0].mediabox
        self.assertEqual((float(box.width), float(box.height)), (288.0, 432.0))
