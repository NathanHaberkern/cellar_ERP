"""GLS adapter, QBO client + connect page, QBO GLS order intake, tracking back to QBO."""
import base64
import datetime as dt
import io
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from integrations import qbo
from integrations.models import QBOConnection
from shipping.carriers import Address, CarrierConfigError, CarrierError, LabelRequest, Parcel
from shipping.carriers.gls import GLSAdapter, to_pdf
from shipping.models import FulfillmentPost, Label, PackPool, ShipOrder
from shipping.services import fulfillment, intake, pools, qbo_intake, shipments
from shipping.services.packing import GLS, OrderData, route

from .fakes import FakeResponse, FakeSession
from .test_intake_and_pools import WEB, FakeAV
from .test_labels_and_fulfillment import PHONE, FakeCarrier, pdf_page

User = get_user_model()
S = ShipOrder.Status


# --- GLS adapter -----------------------------------------------------------------------

class GLSResp(FakeResponse):
    def __init__(self, status_code=200, body=None, headers=None):
        super().__init__(status_code, body)
        self.headers = headers or {}


def gls_session(ship=None, token_status=200):
    def token():
        return GLSResp(token_status, {}, {"Token": "gls-tok"} if token_status == 200 else {})
    ship = ship or (lambda: GLSResp(200, {"ShipmentResponse": {
        "TrackingNumber": "552100001", "TotalCharge": "18.75",
        "ShipmentLabel": base64.b64encode(pdf_page()).decode()}}))
    return FakeSession({("GET", "/token"): token, ("POST", "/Shipment"): ship,
                        ("DELETE", "/Shipment"): lambda: GLSResp(200, {"Result": "Voided"})})


def gls(session=None):
    return GLSAdapter(environment="sandbox", account_number="12345", username="u",
                      password="p", session=session or gls_session())


def gls_req(adult=True):
    frm = Address(name="Shipping", company="St. Amant Winery", street1="1 Winemaster Way",
                  street2="Ste I", city="Lodi", state="CA", postal_code="95240",
                  phone="(209) 555-0199")
    to = Address(name="Receiving", company="Wine Shop", street1="26 Main St", city="Chico",
                 state="CA", postal_code="95973", phone="", residential=False)
    return LabelRequest(ship_from=frm, ship_to=to,
                        parcel=Parcel(Decimal("18.5"), Decimal("14"), Decimal("14.5"),
                                      Decimal("40.9"), reference="SAW-26242"),
                        service="CPS", ship_date=dt.date(2026, 10, 23), adult_signature=adult)


class GLSAdapterTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_payload(self):
        sh = gls().build_ship_payload(gls_req())["Shipment"]
        self.assertEqual((sh["ServiceCode"], sh["SignatureCode"]), ("CPS", "SIG_REQD_ADULT"))
        self.assertEqual((sh["Length"], sh["Height"], sh["Weight"]), (19, 15, 40.9))
        self.assertEqual(sh["DeliveryAddressType"], "B")
        self.assertEqual(sh["ShipperPhone"], "2095550199")
        self.assertEqual(sh["ShipmentReference"], "SAW-26242")
        self.assertEqual(gls().build_ship_payload(gls_req(adult=False))["Shipment"]
                         ["SignatureCode"], "SIG_REQD")

    def test_create_label_token_header_and_parse(self):
        s = gls_session()
        result = gls(s).create_label(gls_req())
        self.assertEqual((result.tracking_number, result.net_charge), ("552100001", Decimal("18.75")))
        self.assertTrue(result.documents[0].content.startswith(b"%PDF"))
        self.assertEqual(s.calls[0][2]["headers"]["AccountNumber"], "12345")
        self.assertEqual(s.calls[1][2]["headers"]["Token"], "gls-tok")

    def test_png_label_becomes_pdf(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (1200, 800), 255).save(buf, format="PNG")
        self.assertTrue(to_pdf(buf.getvalue()).startswith(b"%PDF"))

    def test_errors(self):
        s = gls_session(ship=lambda: GLSResp(400, {"ErrorCode": "E12",
                                                   "ErrorMessage": "Invalid ZIP"}))
        with self.assertRaises(CarrierError) as cm:
            gls(s).create_label(gls_req())
        self.assertEqual((cm.exception.code, str(cm.exception)), ("E12", "[E12] Invalid ZIP"))
        cache.clear()
        with self.assertRaises(CarrierConfigError):
            gls(gls_session(token_status=401)).create_label(gls_req())
        with self.assertRaises(CarrierConfigError):
            GLSAdapter(environment="sandbox", account_number="", username="", password="")

    def test_void(self):
        s = gls_session()
        gls(s).void_label("552100001")
        self.assertEqual(s.calls[-1][2]["params"]["TrackingNumber"], "552100001")


# --- QBO documents ---------------------------------------------------------------------

def invoice(txn_id="17966", doc="SAW-26242", ship_via="GLS", tracking="", addr=None,
            lines=None, customer="Wine Time"):
    return {
        "Id": txn_id, "SyncToken": "1", "DocNumber": doc, "TxnDate": "2026-10-15",
        "CustomerRef": {"value": "61", "name": customer},
        "ShipMethodRef": {"value": ship_via, "name": ship_via} if ship_via else None,
        "TrackingNum": tracking, "ShipDate": "2026-10-23",
        "BillEmail": {"Address": "orders@example.com"},
        "ShipAddr": addr or {"Line1": "26 Lost Dutchman Drive", "City": "Chico",
                             "CountrySubDivisionCode": "CA", "PostalCode": "95973"},
        "Line": lines or [
            {"DetailType": "SalesItemLineDetail", "Description": "2022 Speakeasy Red",
             "SalesItemLineDetail": {"ItemRef": {"name": "22SER"}, "Qty": 2,
                                     "ItemAccountRef": {"name": "40100 Winery Sales:Bottled Wine Sales"}}},
            {"DetailType": "SalesItemLineDetail", "Description": "Vintage Port 2022 12 x 500 ml",
             "SalesItemLineDetail": {"ItemRef": {"name": "22BP"}, "Qty": 1,
                                     "ItemAccountRef": {"name": "40100 Winery Sales:Bottled Wine Sales"}}},
            {"DetailType": "SalesItemLineDetail", "Description": "CA CRV (12 x 750 mL)",
             "SalesItemLineDetail": {"ItemRef": {"name": "CA CRV 9L"}, "Qty": 3,
                                     "ItemAccountRef": {"name": "21210 CA CRV:CA CRV (Distributor Fee)"}}},
            {"DetailType": "SubTotalLineDetail", "Amount": 1},
        ],
    }


class FakeQBO:
    def __init__(self, docs=None, reads=None, update_error=None):
        self.docs = docs or {}
        self.reads = reads or {}
        self.updates = []
        self.update_error = update_error
        self.queries = []

    def query(self, select, entity):
        self.queries.append(select)
        return iter(self.docs.get(entity, []))

    def read(self, entity, txn_id):
        if (entity, txn_id) in self.reads:
            r = self.reads[(entity, txn_id)]
            if isinstance(r, Exception):
                raise r
            return r
        return {"Id": txn_id, "SyncToken": "7"}

    def sparse_update(self, entity, txn_id, sync_token, fields):
        if self.update_error:
            raise self.update_error
        self.updates.append((entity, txn_id, sync_token, fields))
        return {"SyncToken": str(int(sync_token) + 1)}


class AddressParseTests(SimpleTestCase):
    def test_free_form_with_attention_and_company(self):
        out, issues = qbo_intake.parse_address(
            {"Line1": "Pat Buyer", "Line2": "1517 20th Street", "Line3": "Santa Monica, CA 90404"},
            "Mohr-Fry Ranches (C)")
        self.assertEqual(issues, [])
        self.assertEqual((out["ship_name"], out["ship_company"], out["ship_street1"],
                          out["ship_city"], out["ship_state"], out["ship_postal_code"]),
                         ("Pat Buyer", "Mohr-Fry Ranches (C)", "1517 20th Street",
                          "Santa Monica", "CA", "90404"))

    def test_free_form_store_name_suite_and_double_space(self):
        out, issues = qbo_intake.parse_address(
            {"Line1": "Nugget Market #6", "Line2": "2000 Town Center Plaza", "Line3": "Suite 6",
             "Line4": "West Sacramento, CA  95691"}, "Nugget Market #6--West Sacramento")
        self.assertEqual(issues, [])
        self.assertEqual((out["ship_street1"], out["ship_street2"], out["ship_city"]),
                         ("2000 Town Center Plaza", "Suite 6", "West Sacramento"))
        self.assertEqual(out["ship_name"], "Nugget Market #6")

    def test_city_line_only_two_lines_and_zip4_usa(self):
        out, issues = qbo_intake.parse_address(
            {"Line1": "1800 Cavitt Drive", "Line2": "Folsom, CA 95630-1234 USA"}, "Costco")
        self.assertEqual(issues, [])
        self.assertEqual((out["ship_street1"], out["ship_postal_code"], out["ship_name"]),
                         ("1800 Cavitt Drive", "95630", "Costco"))

    def test_structured(self):
        out, issues = qbo_intake.parse_address(
            {"Line1": "4065 McKinley Boulevard", "City": "Sacramento",
             "CountrySubDivisionCode": "CA", "PostalCode": "95819"}, "Compton's Market")
        self.assertEqual((issues, out["ship_street1"], out["ship_city"]),
                         ([], "4065 McKinley Boulevard", "Sacramento"))

    def test_unreadable_goes_to_review(self):
        _, issues = qbo_intake.parse_address({"Line1": "Tuck's Inc.", "Line2": "somewhere"}, "X")
        self.assertTrue(issues)


class LinesAndRoutingTests(TestCase):
    def test_lines_by_case_pack_from_description_and_crv_skipped(self):
        lines = qbo_intake.doc_lines(invoice())
        by_sku = {ln["sku"]: ln for ln in lines}
        self.assertEqual(set(by_sku), {"22SER", "22BP"})
        self.assertEqual((by_sku["22SER"]["quantity"], by_sku["22SER"]["bottles_per_unit"]), (2, 12))
        self.assertEqual((by_sku["22BP"]["ml"], by_sku["22BP"]["bottles_per_unit"]), (500, 12))
        self.assertEqual(by_sku["22SER"]["ml"], 750)             # unknown SKU -> 750 ml

    def test_half_case_counts_bottles(self):
        doc = invoice(lines=[{"DetailType": "SalesItemLineDetail", "Description": "12 x 750 ml",
                              "SalesItemLineDetail": {"ItemRef": {"name": "23MFZ"}, "Qty": 0.5,
                                                      "ItemAccountRef": {"name": "40100 Wine"}}}])
        (ln,) = qbo_intake.doc_lines(doc)
        self.assertEqual((ln["quantity"], ln["bottles_per_unit"]), (6, 1))

    def test_is_gls_matches_names_case_insensitive(self):
        self.assertTrue(qbo_intake.is_gls(invoice(ship_via="gls")))
        self.assertTrue(qbo_intake.is_gls(invoice(ship_via="GSO")))
        self.assertFalse(qbo_intake.is_gls(invoice(ship_via="CUR")))
        self.assertFalse(qbo_intake.is_gls(invoice(ship_via=None)))

    def test_gls_route_ignores_fedex_state_list(self):
        o = OrderData(key="1", number="1", name="A", street1="1 A", city="Bend", state="OR",
                      postal_code="97701", carrier_override=GLS)
        self.assertEqual(route(o, ["OR"])[0], GLS)


def qbo_sync(docs, **kw):
    return qbo_intake.sync_qbo_orders(FakeQBO({"Invoice": docs}, **kw), today=date(2026, 10, 22))


class QBOSyncTests(TestCase):
    def test_queues_only_untracked_gls_documents(self):
        r = qbo_sync([invoice(), invoice("2", "SAW-1", ship_via="CUR"),
                      invoice("3", "SAW-2", tracking="552100009")])
        self.assertEqual(r.created, ["SAW-26242"])
        o = ShipOrder.objects.get()
        self.assertEqual((o.source, o.c7_order_id, o.carrier_override, o.status),
                         ("qbo", "qbo:Invoice:17966", "gls", S.NEW))
        self.assertEqual(o.bottle_count, 36)
        self.assertEqual(o.qbo_ref, ("Invoice", "17966"))
        self.assertEqual(o.requested_ship_date, date(2026, 10, 23))
        self.assertIs(o.ship_residential, False)

    def test_bad_address_to_review_and_resync_updates(self):
        qbo_sync([invoice(addr={"Line1": "Somewhere"})])
        o = ShipOrder.objects.get()
        self.assertEqual(o.status, S.ADDRESS_REVIEW)
        qbo_sync([invoice()])
        o.refresh_from_db()
        self.assertEqual((o.status, o.ship_city), (S.NEW, "Chico"))

    def test_closed_when_tracked_in_qbo_or_deleted(self):
        qbo_sync([invoice(), invoice("2", "SAW-2")])
        r = qbo_sync([], reads={("Invoice", "17966"): {"Id": "17966", "TrackingNum": "X1",
                                                       "ShipMethodRef": {"name": "GLS"}},
                                ("Invoice", "2"): qbo.QBOError("gone", 400)})
        self.assertEqual(sorted(r.closed), ["SAW-2", "SAW-26242"])
        self.assertEqual(ShipOrder.objects.get(c7_order_number="SAW-26242").status, S.FULFILLED)
        self.assertEqual(ShipOrder.objects.get(c7_order_number="SAW-2").status, S.CANCELLED)

    def test_c7_sync_never_closes_qbo_orders(self):
        from .test_intake_and_pools import sync
        qbo_sync([invoice()])
        sync([])
        self.assertEqual(ShipOrder.objects.get().status, S.NEW)

    @override_settings(SHIPPING={**settings.SHIPPING, "COMPLIANCE_GATE": "on"})
    def test_compliance_gate_and_carrier_override_dont_apply(self):
        qbo_sync([invoice()])
        o = ShipOrder.objects.get()
        self.assertEqual(intake.hold_reasons(o, date(2026, 10, 24)), [])
        with self.assertRaises(ValueError):
            intake.set_carrier_override(o, "fedex")


@override_settings(SHIPPING=PHONE)
class QBOPoolAndTrackingTests(TestCase):
    def labeled(self, environment="production"):
        qbo_sync([invoice()])
        for o in ShipOrder.objects.all():
            intake.validate_order(o, FakeAV())
        o = ShipOrder.objects.get()
        o.ship_residential = False
        o.save()
        (pool,), problems = pools.build_pools(today=date(2026, 10, 23))
        self.assertEqual((problems, pool.carrier), ([], "gls"))
        self.assertIn("GLS", pool.name)
        adapter = FakeCarrier(environment=environment)
        adapter.code = "gls"
        bought, errors = shipments.buy_pool_labels(pool, adapter=adapter)
        self.assertEqual((len(bought), errors), (3, []))      # 3 cases -> 3 x 12 Bottle
        self.assertEqual(adapter.requests[0].service, "CPS")
        return pool

    def test_tracking_written_to_qbo_invoice_not_c7(self):
        pool = self.labeled()
        fake = FakeQBO()
        c7 = mock.Mock()
        with mock.patch("shipping.services.qbo_intake.QBOClient", return_value=fake):
            result = fulfillment.post_pool(pool, client=c7)
        self.assertEqual(result.posted, ["SAW-26242"])
        c7.request.assert_not_called()
        ((entity, txn_id, token, fields),) = fake.updates
        self.assertEqual((entity, txn_id, token), ("Invoice", "17966", "7"))
        self.assertEqual(fields["TrackingNum"].count(","), 2)
        self.assertLessEqual(len(fields["TrackingNum"]), 31)
        fp = FulfillmentPost.objects.get()
        self.assertEqual((fp.succeeded, fp.response_summary["target"], fp.send_email),
                         (True, "qbo", False))
        pool.refresh_from_db()
        self.assertEqual(pool.status, PackPool.Status.SHIPPED)

    def test_sandbox_never_written(self):
        pool = self.labeled(environment="sandbox")
        fake = FakeQBO()
        with mock.patch("shipping.services.qbo_intake.QBOClient", return_value=fake):
            result = fulfillment.post_pool(pool)
        self.assertEqual((fake.updates, len(result.skipped)), ([], 1))

    def test_qbo_error_logged(self):
        pool = self.labeled()
        fake = FakeQBO(update_error=qbo.QBOError("Stale object", 400))
        with mock.patch("shipping.services.qbo_intake.QBOClient", return_value=fake):
            result = fulfillment.post_pool(pool)
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(FulfillmentPost.objects.get().succeeded)


# --- QBO client + connect page ------------------------------------------------------------

QBO_CONF = {"ENV": "sandbox", "CLIENT_ID": "cid", "CLIENT_SECRET": "sec",
            "REDIRECT_URI": "http://localhost:8000/integrations/qbo/callback/"}


def token_body(rt="rt-2"):
    return {"access_token": "at-2", "refresh_token": rt, "expires_in": 3600,
            "x_refresh_token_expires_in": 8726400, "token_type": "bearer"}


@override_settings(QBO=QBO_CONF)
class QBOClientTests(TestCase):
    def setUp(self):
        QBOConnection.objects.create(environment="sandbox", realm_id="9130", refresh_token="rt-1")

    def test_refresh_stores_rotated_token_and_queries(self):
        page = {"QueryResponse": {"Invoice": [{"Id": "1"}, {"Id": "2"}]}}
        s = FakeSession({("POST", "/tokens/bearer"): FakeResponse(200, token_body()),
                         ("GET", "/query"): FakeResponse(200, page)})
        with mock.patch("integrations.qbo._token_call",
                        side_effect=lambda data, session=None: token_body()):
            rows = list(qbo.QBOClient(session=s).query("SELECT * FROM Invoice", "Invoice"))
        self.assertEqual([r["Id"] for r in rows], ["1", "2"])
        conn = QBOConnection.objects.get()
        self.assertEqual((conn.refresh_token, conn.access_token), ("rt-2", "at-2"))
        method, url, kw = s.calls[0]
        self.assertIn("/v3/company/9130/query", url)
        self.assertEqual(kw["headers"]["Authorization"], "Bearer at-2")
        self.assertIn("STARTPOSITION 1 MAXRESULTS 1000", kw["params"]["query"])

    def test_expired_refresh_token_says_reconnect(self):
        s = FakeSession({("POST", "/tokens/bearer"):
                         FakeResponse(400, {"error": "invalid_grant"})})
        with self.assertRaisesMessage(qbo.QBOConfigError, "reconnect"):
            qbo.QBOClient(session=s).get("companyinfo/9130")

    def test_valid_access_token_reused(self):
        QBOConnection.objects.update(access_token="at-live",
                                     access_expires_at=timezone.now() + timedelta(minutes=30))
        s = FakeSession({("GET", "/invoice/5"): FakeResponse(200, {"Invoice": {"Id": "5"}})})
        self.assertEqual(qbo.QBOClient(session=s).read("Invoice", "5"), {"Id": "5"})
        self.assertEqual(s.calls[0][2]["headers"]["Authorization"], "Bearer at-live")

    def test_not_connected(self):
        QBOConnection.objects.all().delete()
        with self.assertRaises(qbo.QBOConfigError):
            qbo.QBOClient()


@override_settings(QBO=QBO_CONF, **WEB)
class QBOConnectPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("nate", "n@example.com", "x")

    def test_superuser_only(self):
        self.client.force_login(User.objects.create_user("jill"))
        self.assertEqual(self.client.get(reverse("qbo-status")).status_code, 403)

    def test_connect_and_callback(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("qbo-status")), "Connect QuickBooks")
        resp = self.client.post(reverse("qbo-connect"))
        self.assertTrue(resp["Location"].startswith(qbo.AUTHORIZE_URL))
        state = self.client.session["qbo_oauth_state"]
        bad = self.client.get(reverse("qbo-callback"), {"state": "nope", "code": "c",
                                                        "realmId": "1"}, follow=True)
        self.assertContains(bad, "didn&#x27;t match")

        def fake_connect(code, realm):
            return QBOConnection.objects.create(environment="sandbox", realm_id=realm,
                                                company_name="St. Amant Winery",
                                                refresh_token="rt")
        resp = self.client.post(reverse("qbo-connect"))
        state = self.client.session["qbo_oauth_state"]
        with mock.patch("integrations.qbo.complete_connect", side_effect=fake_connect):
            resp = self.client.get(reverse("qbo-callback"),
                                   {"state": state, "code": "c", "realmId": "9130"}, follow=True)
        self.assertContains(resp, "Connected to St. Amant Winery")

    def test_complete_connect_saves_tokens(self):
        s = FakeSession({("GET", "/companyinfo/9130"): FakeResponse(
            200, {"CompanyInfo": {"CompanyName": "St. Amant Winery"}})})
        with mock.patch("integrations.qbo._token_call",
                        side_effect=lambda data, session=None: token_body("rt-first")):
            conn = qbo.complete_connect("code", "9130", session=s)
        self.assertEqual((conn.realm_id, conn.refresh_token, conn.company_name),
                         ("9130", "rt-first", "St. Amant Winery"))
