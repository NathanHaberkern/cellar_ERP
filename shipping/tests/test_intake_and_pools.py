"""Order sync, address validation, holds, pool build/dissolve, page, replay, daily items."""
import io
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from shipping.carriers import Address, AddressValidation, CarrierError
from shipping.carriers.fedex import parse_address_response
from shipping.models import Label, PackPool, ShipOrder, SkuShippingProfile
from shipping.services import intake, pools, todo


User = get_user_model()
S = ShipOrder.Status
DEFAULT = ["23RTEMP", "24SOUZ", "24IRED", "24TN"]
WEB = dict(SECURE_SSL_REDIRECT=False, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})


def c7_order(number, *, skus=None, state="CA", first="Pat", last="Lee", street="1 Main St",
             zip_="95240", compliance="Compliant", club=True, requested=None, **extra):
    skus = skus or [(s, 1) for s in DEFAULT]
    o = {
        "id": f"id-{number}", "orderNumber": number, "orderPaidDate": "2026-10-10T12:00:00Z",
        "complianceStatus": compliance, "customerId": f"cust-{first}",
        "customer": {"id": f"cust-{first}", "emails": [{"email": "pat@example.com"}],
                     "products": [{"huge": "history"}]},
        "club": ({"clubTitle": "St. Amant Wine Club (4 Bottle Default)",
                  "clubPackageTitle": "Fall 2026 (4-Bottle)"} if club else None),
        "shipTo": {"firstName": first, "lastName": last, "address": street, "address2": "",
                   "city": "Lodi", "stateCode": state, "zipCode": zip_, "countryCode": "US",
                   "phone": "2095550100"},
        "selectedShippingOptions": ({"requestedShipDate": requested} if requested else {}),
        "items": [{"sku": s, "productTitle": s, "quantity": q, "volumeInML": 750,
                   "hasShipping": True} for s, q in skus],
        "fulfillmentStatus": "Not Fulfilled", "orderDeliveryMethod": "Ship",
    }
    o.update(extra)
    return o


class FakeC7:
    """Stands in for Commerce7Client: one page of orders, plus single-order GETs."""

    def __init__(self, orders, singles=None):
        self.orders = orders
        self.singles = singles or {}
        self.params = None

    def iter_pages(self, path, key, params=None, max_records=None):
        self.params = params
        return iter(self.orders)

    def get(self, path, params=None):
        return self.singles.get(path.split("/")[-1], {"fulfillmentStatus": "Fulfilled"})


class FakeAV:
    """Address validation double: per-street canned results; default = clean residential."""

    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls = 0

    def validate_address(self, a: Address):
        self.calls += 1
        if self.error:
            raise self.error
        if a.street1 in self.results:
            return self.results[a.street1]
        return AddressValidation(resolved=True, suggested=a, classification="RESIDENTIAL")


def sync(orders, **kw):
    return intake.sync_orders(FakeC7(orders, **kw), today=date(2026, 10, 21))


class SyncTests(TestCase):
    def test_creates_orders_lines_and_club_fields(self):
        r = sync([c7_order(1001, requested="2026-11-14T01:00:00.000Z")])
        self.assertEqual(r.created, ["1001"])
        o = ShipOrder.objects.get()
        self.assertEqual((o.status, o.ship_name, o.ship_state, o.compliance_status),
                         (S.NEW, "Pat Lee", "CA", "Compliant"))
        self.assertTrue(o.is_club)
        self.assertEqual(o.club_package, "Fall 2026 (4-Bottle)")
        self.assertEqual(o.requested_ship_date, date(2026, 11, 14))
        self.assertEqual(o.ship_email, "pat@example.com")
        self.assertEqual(o.bottle_count, 4)
        self.assertNotIn("products", json.dumps(o.c7_payload))   # customer history trimmed

    def test_params(self):
        c = FakeC7([])
        intake.sync_orders(c, today=date(2026, 10, 21), since=date(2026, 9, 1))
        self.assertEqual(c.params, {"orderPaidDate": "gte:2026-09-01",
                                    "orderDeliveryMethod": "Ship",
                                    "fulfillmentStatus": "Not Fulfilled"})

    def test_resync_updates_lines_and_resets_on_address_change(self):
        sync([c7_order(1001)])
        o = ShipOrder.objects.get()
        o.status = S.READY
        o.address_validated_at = timezone.now()
        o.save()
        sync([c7_order(1001, skus=[("23MFZ", 6)])])
        o.refresh_from_db()
        self.assertEqual(o.status, S.READY)                       # same address: no re-check
        self.assertEqual([(x.sku, x.quantity) for x in o.lines.all()], [("23MFZ", 6)])
        r = sync([c7_order(1001, street="9 New Rd")])
        o.refresh_from_db()
        self.assertEqual((o.status, o.ship_street1), (S.NEW, "9 New Rd"))
        self.assertEqual(r.readdressed, ["1001"])

    def test_pooled_order_never_rewritten_only_flagged(self):
        sync([c7_order(1001)])
        ShipOrder.objects.update(status=S.POOLED)
        r = sync([c7_order(1001, street="9 New Rd")])
        o = ShipOrder.objects.get()
        self.assertEqual(o.ship_street1, "1 Main St")
        self.assertIn("address", o.c7_change_note)
        self.assertEqual(r.flagged, ["1001"])

    def test_missing_order_closed_by_asking_c7(self):
        sync([c7_order(1001), c7_order(1002, first="Sam")])
        r = sync([c7_order(1002, first="Sam")],
                 singles={"id-1001": {"fulfillmentStatus": "Fulfilled"}})
        self.assertEqual(ShipOrder.objects.get(c7_order_number="1001").status, S.FULFILLED)
        self.assertEqual(r.closed, ["1001"])

    def test_old_but_still_open_order_left_alone(self):
        sync([c7_order(1001)])
        sync([], singles={"id-1001": {"fulfillmentStatus": "Not Fulfilled",
                                      "orderDeliveryMethod": "Ship"}})
        self.assertEqual(ShipOrder.objects.get().status, S.NEW)

    def test_bottle_size_override_beats_c7(self):
        SkuShippingProfile.objects.create(sku="MAG23", ml=1500)
        sync([c7_order(1001, skus=[("MAG23", 1)])])
        self.assertEqual(ShipOrder.objects.get().lines.get().ml, 1500)


class AddressValidationTests(TestCase):
    def setUp(self):
        sync([c7_order(1001)])
        self.order = ShipOrder.objects.get()

    def test_clean_goes_ready_with_residential(self):
        intake.validate_order(self.order, FakeAV())
        self.assertEqual((self.order.status, self.order.ship_residential), (S.READY, True))

    def test_formatting_only_difference_is_not_a_change(self):
        sug = Address(name="Pat Lee", street1="1 MAIN ST", city="LODI", state="CA",
                      postal_code="95240-1234")
        av = FakeAV({"1 Main St": AddressValidation(True, sug, "BUSINESS")})
        intake.validate_order(self.order, av)
        self.assertEqual((self.order.status, self.order.ship_residential), (S.READY, False))

    def test_real_change_goes_to_review_and_accept_keeps_original(self):
        sug = Address(name="Pat Lee", street1="1 MAIN ST", city="LODI", state="CA",
                      postal_code="95242")
        intake.validate_order(self.order, FakeAV({"1 Main St": AddressValidation(True, sug)}))
        self.assertEqual(self.order.status, S.ADDRESS_REVIEW)
        self.assertIn("ZIP: 95242", self.order.address_issues[0])
        user = User.objects.create_user("nate")
        intake.accept_suggestion(self.order, user)
        self.assertEqual((self.order.status, self.order.ship_postal_code), (S.READY, "95242"))
        self.assertEqual(self.order.address_original["ship_postal_code"], "95240")
        # A later re-sync of the unchanged C7 address must not look like a C7 edit.
        sync([c7_order(1001)])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, S.READY)

    def test_unresolved_and_carrier_error_go_to_review(self):
        intake.validate_order(self.order, FakeAV({"1 Main St": AddressValidation(
            False, None, issues=["FedEx could not resolve this address"])}))
        self.assertEqual(self.order.status, S.ADDRESS_REVIEW)
        self.order.status = S.NEW
        self.order.save()
        intake.validate_order(self.order, FakeAV(error=CarrierError("boom")))
        self.assertEqual(self.order.status, S.ADDRESS_REVIEW)
        self.assertIn("boom", self.order.address_issues[0])

    def test_edit_rechecks(self):
        user = User.objects.create_user("nate")
        intake.edit_address(self.order, user, {"ship_street1": "2 Main St"}, adapter=FakeAV())
        self.assertEqual((self.order.status, self.order.ship_street1), (S.READY, "2 Main St"))
        self.assertEqual(self.order.address_original["ship_street1"], "1 Main St")


class AddressCheckModeTests(TestCase):
    def setUp(self):
        sync([c7_order(1001)])
        self.order = ShipOrder.objects.get()

    def test_sandbox_skips_check(self):
        with mock.patch("shipping.services.intake.get_adapter") as ga:
            counts = intake.validate_new_orders()
        ga.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, counts["checked"]), (S.READY, False))

    def test_non_us_answer_never_offered(self):
        chile = Address(name="Pat", street1="1 MAIN ST", city="Santiago",
                        state="Región Metropolitana de Santiago", postal_code="95240",
                        country="CL")
        intake.validate_order(self.order, FakeAV({"1 Main St": AddressValidation(True, chile)}))
        self.assertEqual(self.order.status, S.ADDRESS_REVIEW)
        self.assertEqual(self.order.address_suggested, {})
        self.assertIn("non-US", self.order.address_issues[0])

    def test_recheck_clears_queue_when_check_off(self):
        ShipOrder.objects.update(status=S.ADDRESS_REVIEW, address_issues=["junk"])
        counts = intake.recheck_review_queue()
        self.assertEqual(counts["ready"], 1)
        self.assertEqual(ShipOrder.objects.get().status, S.READY)


class FedExParseTests(SimpleTestCase):
    ORIG = Address(name="Pat", street1="1 Main St", street2="Apt 4", city="Lodi", state="CA",
                   postal_code="95240")

    def test_resolved_residential(self):
        body = {"output": {"resolvedAddresses": [{
            "streetLinesToken": ["1 MAIN ST APT 4"], "city": "LODI", "stateOrProvinceCode": "CA",
            "parsedPostalCode": {"base": "95240", "addOn": "1234"}, "countryCode": "US",
            "classification": "RESIDENTIAL",
            "attributes": {"Resolved": "true", "DPV": "true", "SuiteRequiredButMissing": "false"},
            "customerMessages": []}]}}
        v = parse_address_response(body, self.ORIG)
        self.assertTrue(v.resolved)
        self.assertTrue(v.residential)
        self.assertEqual((v.suggested.street1, v.suggested.street2), ("1 MAIN ST APT 4", ""))
        self.assertEqual(v.issues, [])

    def test_suite_missing_and_unresolved(self):
        body = {"output": {"resolvedAddresses": [{
            "streetLinesToken": ["1 MAIN ST"], "classification": "BUSINESS",
            "attributes": {"Resolved": "false", "SuiteRequiredButMissing": "true"}}]}}
        v = parse_address_response(body, self.ORIG)
        self.assertFalse(v.resolved)
        self.assertIs(v.residential, False)
        self.assertIn("Suite/apt number required but missing", v.issues)

    def test_empty(self):
        v = parse_address_response({"output": {}}, self.ORIG)
        self.assertFalse(v.resolved)


class HoldTests(TestCase):
    def setUp(self):
        sync([c7_order(1001, compliance="Quarantined", requested="2026-11-14T01:00:00.000Z")])
        self.order = ShipOrder.objects.get()
        self.order.status = S.READY
        self.order.save()

    def test_requested_date_holds_until_the_day(self):
        self.assertEqual(intake.hold_reasons(self.order, date(2026, 10, 21)), ["Ships on Nov 14"])
        self.assertEqual(intake.hold_reasons(self.order, date(2026, 11, 14)), [])

    def test_compliance_gate_off_ships_everything(self):
        self.assertFalse(any("Compliance" in r for r in
                             intake.hold_reasons(self.order, date(2026, 11, 20))))

    @override_settings(SHIPPING={**__import__("django.conf").conf.settings.SHIPPING,
                                 "COMPLIANCE_GATE": "on"})
    def test_compliance_gate_on_holds_until_override(self):
        self.assertEqual(intake.hold_reasons(self.order, date(2026, 11, 20)),
                         ["Compliance: Quarantined"])
        with self.assertRaises(ValueError):
            intake.override_compliance(self.order, None, "  ")
        intake.override_compliance(self.order, User.objects.create_user("nate"), "VinoCheck ok")
        self.assertEqual(intake.hold_reasons(self.order, date(2026, 11, 20)), [])

    def test_manual_hold_and_release(self):
        intake.put_on_hold(self.order, "customer asked")
        self.assertEqual(self.order.status, S.ON_HOLD)
        intake.release_hold(self.order)
        self.assertEqual(self.order.status, S.NEW)          # never validated -> re-check


class PoolBuildTests(TestCase):
    def ready(self, orders):
        sync(orders)
        for o in ShipOrder.objects.all():
            intake.validate_order(o, FakeAV())

    def test_build_combines_routes_and_dissolves(self):
        self.ready([c7_order(1, first="A", street="1 A St"),
                    c7_order(2, first="B", street="2 B St", state="OR"),
                    c7_order(3, first="C", street="3 C St", skus=[("23MFZ", 4)], club=False),
                    c7_order(4, first="C", street="3 C St", skus=[("24PS", 6)], club=False)])
        made, problems = pools.build_pools(today=date(2026, 10, 21))
        self.assertEqual(problems, [])
        by_name = {p.name: p for p in made}
        self.assertEqual(set(by_name), {"Oct 21 FedEx Default", "Oct 21 UPS Default",
                                        "Oct 21 FedEx 1"})
        combined = by_name["Oct 21 FedEx 1"].packages.get()
        self.assertEqual((combined.box.name, combined.bottle_count), ("12 Bottle", 10))
        self.assertEqual(sorted(combined.orders.values_list("c7_order_number", flat=True)),
                         ["3", "4"])
        ups = ShipOrder.objects.get(c7_order_number="2")
        self.assertEqual((ups.status, ups.carrier), (S.POOLED, "ups"))
        self.assertIn("OR", ups.carrier_reason)

        n = pools.dissolve_pool(by_name["Oct 21 FedEx 1"])
        self.assertEqual(n, 2)
        self.assertEqual(ShipOrder.objects.get(c7_order_number="3").status, S.READY)

    def test_held_orders_left_out(self):
        self.ready([c7_order(1, requested="2026-11-14T01:00:00.000Z")])
        made, _ = pools.build_pools(today=date(2026, 10, 21))
        self.assertEqual(made, [])

    @override_settings(ALLOW_HARD_DELETE=False)
    def test_pool_with_label_cannot_be_dissolved(self):
        self.ready([c7_order(1)])
        (pool,), _ = pools.build_pools(today=date(2026, 10, 21))
        Label.objects.create(package=pool.packages.get(), carrier="fedex", environment="sandbox",
                             service="FEDEX_GROUND", tracking_number="7946", weight_lb=13.9)
        with self.assertRaises(pools.PoolLocked):
            pools.dissolve_pool(pool)


class DailyItemsTests(TestCase):
    def test_due_and_review_and_pool_items(self):
        sync([c7_order(1, requested="2026-10-20T00:00:00.000Z"),
              c7_order(2, first="B", street="2 B St")])
        ShipOrder.objects.filter(c7_order_number="2").update(status=S.ADDRESS_REVIEW)
        PackPool.objects.create(name="Oct 21 FedEx 1", carrier="fedex")
        items = todo.daily_items(date(2026, 10, 21))
        labels = [i["label"] for i in items]
        self.assertIn("Ship #1 (late)", labels)
        self.assertIn("Review 1 shipping address", labels)
        self.assertIn("Pack and label Oct 21 FedEx 1", labels)
        self.assertTrue(all(i["auto_key"] and i["category"] == "Shipping" for i in items))


@override_settings(**WEB)
class ShippingPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("nate", "n@example.com", "x")
        self.packer = User.objects.create_user("jill")
        self.packer.groups.add(Group.objects.get(name="Packer"))
        self.nobody = User.objects.create_user("guest")

    def test_access(self):
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get(reverse("shipping")).status_code, 403)
        self.client.force_login(self.packer)
        resp = self.client.get(reverse("shipping"))
        self.assertContains(resp, "Pull orders")
        self.assertContains(resp, 'href="/shipping/"')

    def test_page_shows_review_ready_and_preview(self):
        sync([c7_order(1, first="A", street="1 A St"), c7_order(2, first="B", street="2 B St")])
        a, b = ShipOrder.objects.order_by("c7_order_number")
        a.status, a.address_issues = S.ADDRESS_REVIEW, ["Suite/apt number required but missing"]
        a.save()
        intake.validate_order(b, FakeAV())
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("shipping"))
        self.assertContains(resp, "Suite/apt number required but missing")
        self.assertContains(resp, "FedEx Default")
        self.assertContains(resp, "Compliance gate: <b>OFF")

    @override_settings(SHIPPING={**__import__("django.conf").conf.settings.SHIPPING,
                                 "ADDRESS_VALIDATION": "on"})
    def test_pull_button(self):
        self.client.force_login(self.packer)
        with mock.patch("shipping.services.intake.Commerce7Client.from_settings",
                        return_value=FakeC7([c7_order(1)])), \
                mock.patch("shipping.services.intake.get_adapter", return_value=FakeAV()):
            resp = self.client.post(reverse("shipping-pull"), follow=True)
        self.assertContains(resp, "1 new")
        self.assertContains(resp, "1 ready, 0 need address review")
        self.assertEqual(ShipOrder.objects.get().status, S.READY)

    def test_build_and_actions_need_permissions(self):
        sync([c7_order(1)])
        o = ShipOrder.objects.get()
        intake.validate_order(o, FakeAV())
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.post(reverse("shipping-build")).status_code, 403)
        self.client.force_login(self.packer)
        self.assertEqual(self.client.post(
            reverse("shipping-order-action", args=[o.pk, "override"]), {"note": "x"}).status_code, 403)
        resp = self.client.post(reverse("shipping-build"), follow=True)
        self.assertContains(resp, "Built 1 pool")
        self.assertEqual(PackPool.objects.count(), 1)

    def test_ship_via_override(self):
        sync([c7_order(1)])
        o = ShipOrder.objects.get()
        intake.validate_order(o, FakeAV())
        self.client.force_login(self.packer)
        self.assertContains(self.client.get(reverse("shipping")), "Ship via")
        self.client.post(reverse("shipping-order-action", args=[o.pk, "carrier"]), {"value": "ups"})
        made, _ = pools.build_pools(today=date(2026, 10, 21))
        self.assertEqual(made[0].carrier, "ups")

    def test_address_edit_form(self):
        sync([c7_order(1)])
        o = ShipOrder.objects.get()
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("shipping-address", args=[o.pk])), "Save")
        with mock.patch("shipping.services.intake.get_adapter", return_value=FakeAV()):
            self.client.post(reverse("shipping-address", args=[o.pk]), {
                "ship_name": "Pat Lee", "ship_street1": "5 Main St", "ship_city": "Lodi",
                "ship_state": "ca", "ship_postal_code": "95240"})
        o.refresh_from_db()
        self.assertEqual((o.ship_street1, o.ship_state, o.status), ("5 Main St", "CA", S.READY))


class ReplayCommandTests(TestCase):
    def test_compares_boxes_combining_and_carrier(self):
        def shipped(o, tracking, count=1, carrier="FedEx"):
            o = dict(o, fulfillmentStatus="Fulfilled")
            o["fulfillments"] = [{"type": "Shipped", "packageCount": count,
                                  "shipped": {"trackingNumbers": tracking, "carrier": carrier}}]
            return o
        orders = [
            shipped(c7_order(1, first="A", street="1 A St"), ["T1"]),
            shipped(c7_order(2, first="C", street="3 C St", skus=[("23MFZ", 4)]), ["T2"]),
            shipped(c7_order(3, first="C", street="3 C St", skus=[("24PS", 6)]), ["T2"]),
            shipped(c7_order(4, first="D", street="4 D St", state="OR"), ["T3"], carrier="FedEx"),
            shipped(c7_order(5, first="E", street="5 E St", skus=[("23MFZ", 24)]), ["T4"]),
        ]
        fake = FakeC7(orders)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("shipping.management.commands.shipping_replay.Commerce7Client"
                           ".from_settings", return_value=fake):
            out = Path(d) / "r.txt"
            call_command("shipping_replay", "--out", str(out), stdout=io.StringIO())
            text = out.read_text(encoding="utf-8")
        self.assertEqual(fake.params["fulfillmentStatus"], "Fulfilled")
        self.assertEqual(fake.params["channel"], "Club")
        self.assertIn("BOX COUNT  3/4 shipments match", text)     # #5: app 2 boxes vs C7 1
        self.assertIn("#5 CA: app 2", text)
        self.assertIn("COMBINING  0 difference(s)", text)        # #2/#3 combined both ways
        self.assertIn("CARRIER  1 difference(s)", text)          # #4 OR shipped FedEx in C7
        self.assertIn("POOLS", text)
        self.assertNotIn("1 A St", text)                          # no street addresses
