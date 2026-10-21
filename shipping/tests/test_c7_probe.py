"""C7 client paging/auth and the probe's scrubbing -- no network."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from integrations.commerce7 import C7ConfigError, C7Error, Commerce7Client
from shipping.management.commands.c7_probe import census, scrub

from .fakes import FakeResponse

C7 = {"C7": {"APP_ID": "app", "APP_SECRET": "sec", "TENANT_ID": "st-amant"}}

ORDER = {
    "id": "o1", "orderNumber": 1001, "complianceStatus": "Compliant", "channel": "Club",
    "fulfillmentStatus": "Not Fulfilled", "orderDeliveryMethod": "Ship",
    "club": {"clubId": "c1", "clubTitle": "Port Club"},
    "customer": {"firstName": "Jane", "lastName": "Doe", "emails": [{"email": "j@x.com"}],
                 "birthDate": "1970-01-01"},
    "shipTo": {"firstName": "Jane", "lastName": "Doe", "address": "1 Main St", "city": "Austin",
               "stateCode": "TX", "zipCode": "78701", "countryCode": "US", "phone": "5125550100"},
    "shippingInstructions": "leave at back door",
    "items": [{"sku": "24TN", "productTitle": "Port Club Volume 12", "quantity": 1}],
}


class SeqSession:
    """requests.Session stand-in replaying a list of responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.auth = None

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


class ClientTests(SimpleTestCase):
    def test_missing_credentials_named(self):
        with self.assertRaisesMessage(C7ConfigError, "C7_APP_SECRET, C7_TENANT_ID"):
            Commerce7Client(app_id="a", app_secret="", tenant_id="")

    def test_auth_and_tenant_header(self):
        s = SeqSession([])
        Commerce7Client(app_id="a", app_secret="b", tenant_id="t", session=s)
        self.assertEqual(s.auth, ("a", "b"))
        self.assertEqual(s.headers["tenant"], "t")

    def test_cursor_paging_and_limit(self):
        s = SeqSession([FakeResponse(200, {"orders": [{"id": 1}, {"id": 2}], "cursor": "abc"}),
                        FakeResponse(200, {"orders": [{"id": 3}], "cursor": None})])
        c = Commerce7Client(app_id="a", app_secret="b", tenant_id="t", session=s)
        self.assertEqual([o["id"] for o in c.iter_pages("order", "orders", {"x": "1"})], [1, 2, 3])
        self.assertEqual(s.calls[0][2]["params"]["cursor"], "start")
        self.assertEqual(s.calls[1][2]["params"]["cursor"], "abc")

        s2 = SeqSession([FakeResponse(200, {"orders": [{"id": 1}, {"id": 2}], "cursor": "abc"})])
        c2 = Commerce7Client(app_id="a", app_secret="b", tenant_id="t", session=s2)
        self.assertEqual(len(list(c2.iter_pages("order", "orders", max_records=1))), 1)

    def test_401_hint(self):
        s = SeqSession([FakeResponse(401, {"message": "Unauthorized"})])
        c = Commerce7Client(app_id="a", app_secret="b", tenant_id="t", session=s)
        with self.assertRaisesMessage(C7Error, "check C7_APP_ID"):
            c.get("order")


class ScrubTests(SimpleTestCase):
    def test_pii_removed_routing_fields_kept(self):
        out = scrub(ORDER)
        blob = json.dumps(out)
        for secret in ("Jane", "Doe", "j@x.com", "1 Main St", "Austin", "78701",
                       "5125550100", "1970-01-01", "back door"):
            self.assertNotIn(secret, blob)
        self.assertEqual(out["shipTo"]["stateCode"], "TX")
        self.assertEqual(out["complianceStatus"], "Compliant")
        self.assertEqual(out["club"]["clubTitle"], "Port Club")
        self.assertEqual(out["items"][0]["productTitle"], "Port Club Volume 12")
        self.assertEqual(out["items"][0]["quantity"], 1)

    def test_census(self):
        c = census([scrub(ORDER)])
        self.assertEqual(c["complianceStatus"]["values"], ['"Compliant"'])
        self.assertIn("items[].sku", c)


@override_settings(SHIPPING=C7)
class ProbeCommandTests(SimpleTestCase):
    def test_writes_scrubbed_file(self):
        s = SeqSession([FakeResponse(200, {"orders": [ORDER], "cursor": None})])
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("integrations.commerce7.requests.Session", return_value=s):
            out = Path(d) / "p.json"
            call_command("c7_probe", "--out", str(out), "--param", "channel=Club", stdout=mock.MagicMock())
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["order_count"], 1)
        self.assertNotIn("Jane", json.dumps(data))
        p = s.calls[0][2]["params"]
        self.assertEqual(p["orderDeliveryMethod"], "Ship")
        self.assertEqual(p["fulfillmentStatus"], "Not Fulfilled")
        self.assertEqual(p["channel"], "Club")
        self.assertTrue(p["orderPaidDate"].startswith("gte:"))

    @override_settings(SHIPPING={"C7": {"APP_ID": "", "APP_SECRET": "", "TENANT_ID": ""}})
    def test_missing_creds_is_command_error(self):
        with self.assertRaises(CommandError):
            call_command("c7_probe", stdout=mock.MagicMock())
