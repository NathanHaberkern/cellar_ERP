"""Tranche 4: buy/void/print a pool's labels, post tracking to C7, the pool page."""
import io
from datetime import date
from decimal import Decimal
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from integrations.commerce7 import C7Error
from shipping.carriers import CarrierAdapter, CarrierError, LabelDocumentData, LabelResult
from shipping.models import FulfillmentPost, Label, PackPool, ShipOrder
from shipping.services import fulfillment, intake, pools, shipments
from shipping.services.labels import LabelRefused

from .test_intake_and_pools import WEB, FakeAV, c7_order, sync

User = get_user_model()
S = ShipOrder.Status
P = PackPool.Status
PHONE = {**settings.SHIPPING, "SHIP_FROM": {**settings.SHIPPING["SHIP_FROM"],
                                            "phone": "2095550199"}}


def pdf_page():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(288, 432))
    c.drawString(20, 400, "label")
    c.showPage()
    c.save()
    return buf.getvalue()


class FakeCarrier(CarrierAdapter):
    """Buys numbered labels; can be told to refuse particular references."""
    code = "fedex"

    def __init__(self, environment="production", refuse=()):
        super().__init__(environment)
        self.refuse = set(refuse)
        self.requests, self.voided = [], []

    def create_label(self, request):
        self.requests.append(request)
        if request.parcel.reference in self.refuse:
            raise CarrierError("Recipient postal code invalid", code="POSTAL.INVALID")
        n = len(self.requests)
        return LabelResult(carrier=self.code, environment=self.environment,
                           service=request.service, tracking_number=f"7946{n:08d}",
                           net_charge=Decimal("14.20"), currency="USD",
                           documents=[LabelDocumentData(kind="LABEL", content=pdf_page())])

    def void_label(self, tracking_number):
        self.voided.append(tracking_number)
        return {"ok": True}


class FakeC7Post:
    def __init__(self, fail=None, has=()):
        self.fail = fail
        self.has = list(has)
        self.posts = []

    def request(self, method, path, json=None, params=None):
        if self.fail:
            raise self.fail
        self.posts.append((method, path, json))
        return {"id": "ful-1"}

    def get(self, path, params=None):
        return {"fulfillments": [{"type": "Shipped",
                                  "shipped": {"trackingNumbers": self.has}}]}


def with_item_ids(o):
    for i, item in enumerate(o["items"]):
        item["id"] = f"{o['id']}-item-{i}"
    return o


class PoolFixture(TestCase):
    def make_pool(self, orders=None):
        orders = orders or [
            with_item_ids(c7_order(1, first="A", street="1 A St")),
            with_item_ids(c7_order(3, first="C", street="3 C St", skus=[("23MFZ", 4)], club=False)),
            with_item_ids(c7_order(4, first="C", street="3 C St", skus=[("24PS", 6)], club=False)),
        ]
        sync(orders)
        for o in ShipOrder.objects.all():
            intake.validate_order(o, FakeAV())
        made, problems = pools.build_pools(today=date(2026, 10, 21))
        self.assertEqual(problems, [])
        return made


@override_settings(SHIPPING=PHONE)
class LabelBuyingTests(PoolFixture):
    def test_label_request_uses_box_weight_address_and_service(self):
        pool = next(p for p in self.make_pool() if not p.is_default)
        package = pool.packages.get()                       # #3 + #4 combined
        req = shipments.label_request_for(package, ship_date=date(2026, 10, 22))
        self.assertEqual(req.parcel.reference, "3/4")
        self.assertEqual((req.parcel.length_in, req.parcel.weight_lb),
                         (package.box.length_in, package.weight_lb))
        self.assertEqual(req.ship_to.street1, "3 C St")
        self.assertEqual(req.ship_to.email, "")             # C7 sends the ship email, not FedEx
        self.assertEqual(req.service, "GROUND_HOME_DELIVERY")   # FakeAV says residential
        self.assertTrue(req.contains_alcohol and req.adult_signature)

    def test_buy_pool_labels_then_statuses_and_print(self):
        pools_ = self.make_pool()
        adapter = FakeCarrier()
        for pool in pools_:
            bought, errors = shipments.buy_pool_labels(pool, adapter=adapter)
            self.assertEqual(errors, [])
            pool.refresh_from_db()
            self.assertEqual(pool.status, P.LABELED)
        self.assertEqual(set(ShipOrder.objects.values_list("status", flat=True)), {S.LABELED})
        # buying again buys nothing
        self.assertEqual(shipments.buy_pool_labels(pools_[0], adapter=adapter)[0], [])
        pdf = shipments.merged_pdf(shipments.pool_live_labels(pools_[0]))
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), pools_[0].packages.count())

    def test_refused_package_doesnt_stop_the_rest(self):
        made = self.make_pool([with_item_ids(c7_order(n, first=f"P{n}", street=f"{n} X St",
                                                      skus=[("23MFZ", 4)], club=False))
                               for n in (11, 12)])
        pool = made[0]
        self.assertEqual(pool.packages.count(), 2)
        bought, errors = shipments.buy_pool_labels(pool, adapter=FakeCarrier(refuse={"11"}))
        self.assertEqual((len(bought), len(errors)), (1, 1))
        self.assertIn("postal code", errors[0][1])
        pool.refresh_from_db()
        self.assertEqual(pool.status, P.OPEN)
        statuses = dict(ShipOrder.objects.values_list("c7_order_number", "status"))
        self.assertEqual(statuses, {"11": S.POOLED, "12": S.LABELED})

    def test_c7_change_blocks_buying(self):
        pool = self.make_pool()[0]
        ShipOrder.objects.update(c7_change_note="C7 changed address after packing started")
        with self.assertRaises(LabelRefused):
            shipments.buy_pool_labels(pool, adapter=FakeCarrier())

    @override_settings(SHIPPING={**PHONE, "SHIP_FROM": {**PHONE["SHIP_FROM"], "phone": ""}})
    def test_ship_from_phone_required(self):
        pool = self.make_pool()[0]
        with self.assertRaises(LabelRefused):
            shipments.buy_pool_labels(pool, adapter=FakeCarrier())

    def test_void_reopens_pool_and_order(self):
        pool = self.make_pool()[0]
        adapter = FakeCarrier()
        shipments.buy_pool_labels(pool, adapter=adapter)
        label = Label.objects.filter(package__pool=pool).first()
        shipments.void_package_label(label, adapter=adapter)
        pool.refresh_from_db()
        self.assertEqual(pool.status, P.OPEN)
        self.assertEqual(adapter.voided, [label.tracking_number])
        order = label.package.orders.first()
        order.refresh_from_db()
        self.assertEqual(order.status, S.POOLED)


@override_settings(SHIPPING=PHONE)
class FulfillmentTests(PoolFixture):
    def labeled_pool(self, environment="production"):
        pool = next(p for p in self.make_pool() if not p.is_default)   # #3/#4, one box
        shipments.buy_pool_labels(pool, adapter=FakeCarrier(environment=environment))
        return pool

    def test_posts_every_order_in_a_combined_box_with_items(self):
        pool = self.labeled_pool()
        c7 = FakeC7Post()
        result = fulfillment.post_pool(pool, client=c7)
        self.assertEqual(result.posted, ["3", "4"])
        tracking = Label.objects.get(package__pool=pool).tracking_number
        method, path, body = c7.posts[0]
        self.assertEqual((method, path), ("POST", "order/id-3/fulfillment"))
        self.assertEqual(body["type"], "Shipped")
        self.assertTrue(body["sendTransactionEmail"])
        self.assertEqual(body["shipped"], {"carrier": "FedEx", "trackingNumbers": [tracking]})
        self.assertEqual(body["items"], [{"orderItemId": "id-3-item-0", "quantity": 4}])
        self.assertEqual(c7.posts[1][2]["shipped"]["trackingNumbers"], [tracking])
        pool.refresh_from_db()
        self.assertEqual(pool.status, P.SHIPPED)
        self.assertEqual(FulfillmentPost.objects.filter(succeeded=True).count(), 2)
        # second press posts nothing
        again = fulfillment.post_pool(pool, client=c7)
        self.assertEqual((again.posted, len(c7.posts)), ([], 2))

    def test_sandbox_labels_never_posted(self):
        pool = self.labeled_pool(environment="sandbox")
        c7 = FakeC7Post()
        result = fulfillment.post_pool(pool, client=c7)
        self.assertEqual(c7.posts, [])
        self.assertIn("sandbox", result.skipped[0][1])
        self.assertFalse(FulfillmentPost.objects.exists())

    def test_writeback_off_posts_nothing(self):
        pool = self.labeled_pool()
        c7 = FakeC7Post()
        with override_settings(SHIPPING={**PHONE, "C7_WRITEBACK": "off"}):
            result = fulfillment.post_pool(pool, client=c7)
        self.assertEqual((c7.posts, len(result.skipped)), ([], 2))

    def test_c7_refusal_logged_and_order_stays_labeled(self):
        pool = self.labeled_pool()
        c7 = FakeC7Post(fail=C7Error("C7 POST -> 422", 422, {"message": "already fulfilled"}))
        result = fulfillment.post_pool(pool, client=c7)
        self.assertEqual(len(result.errors), 2)
        fp = FulfillmentPost.objects.first()
        self.assertEqual((fp.succeeded, fp.http_status), (False, 422))
        self.assertEqual(ShipOrder.objects.get(c7_order_number="3").status, S.LABELED)

    def test_retry_after_timeout_checks_c7_first(self):
        pool = self.labeled_pool()
        order = ShipOrder.objects.get(c7_order_number="3")
        outcome, _ = fulfillment.post_order(order, client=FakeC7Post(fail=requests.Timeout()))
        self.assertEqual(outcome, "error")
        tracking = Label.objects.get(package__pool=pool).tracking_number
        c7 = FakeC7Post(has=[tracking])                    # C7 got it after all
        outcome, msg = fulfillment.post_order(order, client=c7)
        self.assertEqual((outcome, c7.posts), ("posted", []))   # no second email
        order.refresh_from_db()
        self.assertEqual(order.status, S.FULFILLED)

    def test_void_refused_once_posted(self):
        pool = self.labeled_pool()
        fulfillment.post_pool(pool, client=FakeC7Post())
        label = Label.objects.get(package__pool=pool)
        with self.assertRaises(LabelRefused):
            shipments.void_package_label(label, adapter=FakeCarrier())
        label.refresh_from_db()
        self.assertIsNone(label.voided_at)


@override_settings(SHIPPING=PHONE, **WEB)
class PoolPageTests(PoolFixture):
    def setUp(self):
        self.owner = User.objects.create_superuser("nate", "n@example.com", "x")
        self.packer = User.objects.create_user("jill")
        self.packer.groups.add(Group.objects.get(name="Packer"))

    def test_buy_print_post_through_the_page(self):
        pool = next(p for p in self.make_pool() if not p.is_default)
        self.client.force_login(self.packer)
        resp = self.client.get(reverse("shipping-pool", args=[pool.pk]))
        self.assertContains(resp, "Buy 1 label")
        self.assertContains(resp, "TEST keys")                 # settings default: sandbox
        adapter = FakeCarrier()
        with mock.patch("shipping.services.shipments.get_adapter", return_value=adapter):
            resp = self.client.post(reverse("shipping-pool-buy", args=[pool.pk]), follow=True)
        self.assertContains(resp, "Bought 1 label")
        resp = self.client.get(reverse("shipping-pool-pdf", args=[pool.pk]))
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))
        label = Label.objects.get(package__pool=pool)
        self.assertEqual(self.client.get(reverse("shipping-label-pdf", args=[label.pk]))
                         .status_code, 200)
        # Packer can't void
        self.assertEqual(self.client.post(reverse("shipping-label-void", args=[label.pk]))
                         .status_code, 403)
        c7 = FakeC7Post()
        with mock.patch("shipping.services.fulfillment.Commerce7Client.from_settings",
                        return_value=c7):
            resp = self.client.post(reverse("shipping-pool-post", args=[pool.pk]), follow=True)
        self.assertContains(resp, "2 posted")
        self.assertEqual(len(c7.posts), 2)

    def test_owner_voids(self):
        pool = self.make_pool()[0]
        adapter = FakeCarrier()
        shipments.buy_pool_labels(pool, adapter=adapter)
        label = Label.objects.filter(package__pool=pool).first()
        self.client.force_login(self.owner)
        with mock.patch("shipping.services.labels.get_adapter", return_value=adapter):
            resp = self.client.post(reverse("shipping-label-void", args=[label.pk]), follow=True)
        self.assertContains(resp, f"Voided {label.tracking_number}")
        self.assertContains(resp, "<s>")

    def test_index_links_pool(self):
        pool = self.make_pool()[0]
        self.client.force_login(self.packer)
        self.assertContains(self.client.get(reverse("shipping")),
                            reverse("shipping-pool", args=[pool.pk]))
