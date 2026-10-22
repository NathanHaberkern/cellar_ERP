from datetime import date, datetime, timezone as tz

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse

from clubdesk.models import C7Write, ClubRequest, Kind
from clubdesk.services import apply as applier
from clubdesk.services import tax
from clubdesk.services.plan import Facts, OpenOrder, plan
from clubdesk.services.propose import apply_pending_dates, load, propose
from integrations.commerce7 import C7Error
from shipping.models import ShipOrder

S = ClubRequest.Status
TODAY = date(2026, 10, 22)
LODI = tax.TaxPlace("CA", "95240")
SF = tax.TaxPlace("CA", "94110")


class FakeC7:
    """Records calls; answers like Commerce7. fail_on=(method, path prefix) raises."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def request(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        if self.fail_on and method == self.fail_on[0] and path.startswith(self.fail_on[1]):
            raise C7Error(f"C7 {method} /{path} -> 400", 400, {"message": "bad"})
        if path == "customer-address":
            return {"id": "addr-new"}
        return {"id": "x"}

    def get(self, path, params=None):
        return self.request("GET", path, params=params)


def make_req(kind, **kw):
    data = dict(mailbox="info@", message_id=f"m-{kind}-{ClubRequest.objects.count()}",
                received_at=datetime(2026, 10, 20, 15, tzinfo=tz.utc), customer_email="pat@example.com",
                customer_name="Pat Lee", first_name="Pat", kind=kind)
    data.update(kw)
    return ClubRequest.objects.create(**data)


def facts(**kw):
    base = dict(today=TODAY, customer_id="cust-1", membership_id="mem-1", membership_status="Active",
                taxed_states=frozenset({"CA", "NV", "TX"}))
    base.update(kw)
    return Facts(**base)


def ship_order(pk_num="61001", status="ready", **kw):
    data = dict(c7_order_id=f"o-{pk_num}", c7_order_number=pk_num, ship_name="Pat Lee",
                ship_street1="1 Main St", ship_city="San Francisco", ship_state="CA",
                ship_postal_code="94110", c7_customer_id="cust-1", is_club=True,
                club_package="Fall 2026 (4-Bottle)", status=status,
                c7_payload={"paymentStatus": "Paid", "orderDeliveryMethod": "Ship"})
    data.update(kw)
    return ShipOrder.objects.create(**data)


def open_order(o, paid=True, delivery="Ship", taxed=SF):
    return OpenOrder(order_number=o.c7_order_number, ship_order_pk=o.pk, ship_status=o.status,
                     paid=paid, delivery=delivery, taxed_at=taxed, package=o.club_package)


class TaxGuardrailTests(TestCase):
    def test_future_only_change_is_allowed(self):
        self.assertEqual(tax.check_change(tax.TaxPlace("NV", "89501"), [], {"CA", "NV"}), [])

    def test_paid_order_to_other_zip_is_blocked(self):
        reasons = tax.check_change(LODI, [("61001", SF)], {"CA"})
        self.assertEqual(len(reasons), 1)
        self.assertIn("under- or over-collect", reasons[0])

    def test_paid_order_same_zip_is_allowed(self):
        self.assertEqual(tax.check_change(SF, [("61001", SF)], {"CA"}), [])

    def test_untaxed_state_is_blocked(self):
        self.assertIn("no tax set up for UT", tax.check_change(tax.TaxPlace("UT", "84101"), [], {"CA"})[0])


class PlanTests(TestCase):
    def test_delay_before_build_saves_date_and_notes_it(self):
        req = make_req(Kind.DELAY, requested_date=date(2026, 11, 2), details={"package": "Winter 2027"})
        p = plan(req, facts())
        self.assertEqual(p.stage, "before_build")
        self.assertEqual([a["target"] for a in p.actions], ["pending_date"])
        self.assertIn("11/2/2026", p.note_text)
        self.assertIn("Winter 2027", p.note_text)

    def test_delay_after_build_sets_ship_date(self):
        o = ship_order()
        req = make_req(Kind.DELAY, requested_date=date(2026, 10, 26))
        p = plan(req, facts(open_orders=[open_order(o)]))
        self.assertEqual(p.actions[0]["set"], {"requested_ship_date": "2026-10-26"})
        self.assertEqual(p.block_reasons, [])

    def test_delay_on_labeled_order_is_blocked(self):
        o = ship_order(status="labeled")
        p = plan(make_req(Kind.DELAY, requested_date=date(2026, 10, 26)),
                 facts(open_orders=[open_order(o)]))
        self.assertIn("already labeled", p.block_reasons[0])

    def test_cancel_puts_membership_and_holds_order(self):
        o = ship_order()
        p = plan(make_req(Kind.CANCEL, details={"reason": "Moving"}), facts(open_orders=[open_order(o)]))
        put = p.actions[0]
        self.assertEqual(put["path"], "club-membership/mem-1")
        self.assertEqual(put["body"]["status"], "Cancelled")
        self.assertEqual(put["body"]["cancellationReason"], "Moving")
        self.assertFalse(put["body"]["isSendEmailConfirmation"])
        self.assertTrue(any("Refund order #61001" in m for m in p.manual_steps))

    def test_skip_needs_resume_date_and_is_marked_unverified(self):
        self.assertTrue(plan(make_req(Kind.SKIP), facts()).block_reasons)
        p = plan(make_req(Kind.SKIP, requested_date=date(2027, 2, 15)), facts())
        self.assertEqual(p.actions[0]["unverified"], "onHolds")

    def test_pickup_on_paid_shipped_order_hits_tax_guardrail(self):
        o = ship_order()
        p = plan(make_req(Kind.HOLD_PICKUP), facts(open_orders=[open_order(o)]))
        self.assertTrue(any("tax" in b for b in p.block_reasons))

    def test_address_change_with_paid_order_elsewhere_is_blocked(self):
        o = ship_order()
        addr = {"street1": "9 Oak Ave", "city": "Reno", "state": "NV", "zip": "89501"}
        p = plan(make_req(Kind.ADDRESS, details={"address": addr}), facts(open_orders=[open_order(o)]))
        self.assertTrue(any("#61001" in b for b in p.block_reasons))

    def test_address_change_future_only_creates_address_then_points_membership(self):
        addr = {"street1": "9 Oak Ave", "city": "Reno", "state": "NV", "zip": "89501"}
        p = plan(make_req(Kind.ADDRESS, details={"address": addr}), facts())
        self.assertEqual(p.block_reasons, [])
        self.assertEqual([a["path"] for a in p.actions], ["customer-address", "club-membership/mem-1"])
        self.assertEqual(p.actions[1]["body"]["shipToCustomerAddressId"], "{address_id}")

    def test_swap_before_build_is_blocked_until_route_known(self):
        p = plan(make_req(Kind.SWAP, details={"add": ["Zinfandel Reserve"]}), facts())
        self.assertIn("club-package route", p.block_reasons[0])

    def test_no_membership_blocks_membership_changes(self):
        self.assertTrue(plan(make_req(Kind.CANCEL), facts(membership_id="")).block_reasons)


class ApplyTests(TestCase):
    def setUp(self):
        self.nate = User.objects.create_user("nate", first_name="Nate", last_name="H")
        self.nate.user_permissions.add(Permission.objects.get(codename="approve_club_changes"))
        self.clerk = User.objects.create_user("clerk")

    def _approved(self, req, f):
        propose(req, None, facts=f)
        req.refresh_from_db()
        req.status, req.approved_by = S.APPROVED, self.nate
        req.save()
        return req

    def test_unapproved_request_is_refused(self):
        req = make_req(Kind.CANCEL)
        propose(req, None, facts=facts())
        with self.assertRaises(applier.NotApproved):
            applier.apply(req, self.nate, FakeC7())

    def test_user_without_permission_is_refused(self):
        req = self._approved(make_req(Kind.CANCEL), facts())
        with self.assertRaises(applier.NotApproved):
            applier.apply(req, User.objects.get(username="clerk"), FakeC7())

    def test_cancel_writes_membership_then_note(self):
        o = ship_order()
        req = self._approved(make_req(Kind.CANCEL), facts(open_orders=[open_order(o)]))
        c7 = FakeC7()
        self.assertTrue(applier.apply(req, self.nate, c7))
        self.assertEqual([(m, p) for m, p, _ in c7.calls], [("PUT", "club-membership/mem-1"),
                                                            ("POST", "note")])
        note = c7.calls[-1][2]
        self.assertEqual(note["customerId"], "cust-1")
        self.assertIn("Approved by Nate H", note["content"])
        o.refresh_from_db()
        self.assertEqual(o.status, "on_hold")
        self.assertEqual(C7Write.objects.filter(request=req, ok=True).count(), 2)

    def test_address_id_from_first_call_feeds_membership(self):
        addr = {"street1": "9 Oak Ave", "city": "Reno", "state": "NV", "zip": "89501"}
        req = self._approved(make_req(Kind.ADDRESS, details={"address": addr}), facts())
        c7 = FakeC7()
        applier.apply(req, self.nate, c7)
        self.assertEqual(c7.calls[1][2]["shipToCustomerAddressId"], "addr-new")

    def test_failed_write_stops_and_skips_note(self):
        req = self._approved(make_req(Kind.CANCEL), facts())
        c7 = FakeC7(fail_on=("PUT", "club-membership"))
        self.assertFalse(applier.apply(req, self.nate, c7))
        req.refresh_from_db()
        self.assertEqual(req.status, S.FAILED)
        self.assertEqual(len(c7.calls), 1)

    def test_failed_note_marks_request_failed(self):
        req = self._approved(make_req(Kind.CANCEL), facts())
        self.assertFalse(applier.apply(req, self.nate, FakeC7(fail_on=("POST", "note"))))
        req.refresh_from_db()
        self.assertEqual(req.status, S.FAILED)

    def test_unverified_shape_is_refused(self):
        req = make_req(Kind.SKIP, requested_date=date(2027, 2, 15))
        req = self._approved(req, facts())
        with self.assertRaises(applier.NotApproved):
            applier.apply(req, self.nate, FakeC7())
        with override_settings(CLUBDESK={"PICKUP_STATE": "CA", "PICKUP_ZIP": "95240",
                                         "VERIFIED_SHAPES": ["onHolds"]}):
            self.assertTrue(applier.apply(req, self.nate, FakeC7()))

    def test_delay_only_writes_the_note(self):
        req = self._approved(make_req(Kind.DELAY, requested_date=date(2026, 11, 2)), facts())
        c7 = FakeC7()
        self.assertTrue(applier.apply(req, self.nate, c7))
        self.assertEqual([p for _, p, _ in c7.calls], ["note"])
        self.assertIn("11/2/2026", c7.calls[0][2]["content"])

    def test_pending_date_lands_on_built_order(self):
        req = self._approved(make_req(Kind.DELAY, requested_date=date(2026, 11, 2)), facts())
        applier.apply(req, self.nate, FakeC7())
        o = ship_order()
        self.assertEqual(apply_pending_dates(), [o])
        o.refresh_from_db()
        self.assertEqual(o.requested_ship_date, date(2026, 11, 2))

    def test_c7write_rows_cannot_be_edited(self):
        req = make_req(Kind.OTHER)
        w = C7Write.objects.create(request=req, method="POST", path="note")
        with self.assertRaises(ValueError):
            w.save()


class DraftAndLoadTests(TestCase):
    def test_draft_matches_nate_style(self):
        req = make_req(Kind.DELAY, requested_date=date(2026, 10, 26))
        propose(req, None, facts=facts())
        self.assertEqual(req.draft_text.split("\n")[0], "Pat,")
        self.assertIn("You've got it! We will have your wine delivered after 10/26.", req.draft_text)
        self.assertTrue(req.draft_text.endswith(
            "Thanks,\n\n\nNathan Haberkern\nSt. Amant Winery\n(209) 367-0646\nnathan@stamantwine.com"))
        for banned in ("Best regards", "don't hesitate", "Dear"):
            self.assertNotIn(banned, req.draft_text)

    def test_far_date_uses_month_name(self):
        req = make_req(Kind.SKIP, requested_date=date(2027, 2, 15))
        propose(req, None, facts=facts())
        self.assertIn("reach out again in February.", req.draft_text)

    def test_load_skips_duplicates(self):
        rec = {"mailbox": "info@", "message_id": "abc", "received_at": "2026-10-20T10:00:00Z",
               "customer_email": "a@b.com", "kind": "skip", "requested_date": "2027-02-01"}
        self.assertEqual(len(load([rec, rec])), 1)
        self.assertEqual(len(load([rec])), 0)


WEB = dict(SECURE_SSL_REDIRECT=False, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})


@override_settings(**WEB)
class PageTests(TestCase):
    def test_approve_then_apply_buttons_need_permission(self):
        req = make_req(Kind.CANCEL)
        propose(req, None, facts=facts())
        clerk = User.objects.create_user("clerk2")
        clerk.user_permissions.add(Permission.objects.get(codename="view_club_requests"))
        self.client.force_login(clerk)
        self.assertEqual(self.client.get(reverse("clubdesk")).status_code, 200)
        r = self.client.post(reverse("clubdesk-action", args=[req.pk, "approve"]))
        self.assertEqual(r.status_code, 403)
        nate = User.objects.create_user("nate2")
        nate.user_permissions.add(Permission.objects.get(codename="approve_club_changes"))
        self.client.force_login(nate)
        self.client.post(reverse("clubdesk-action", args=[req.pk, "approve"]))
        req.refresh_from_db()
        self.assertEqual(req.status, S.APPROVED)
        self.assertEqual(req.approved_by, nate)
