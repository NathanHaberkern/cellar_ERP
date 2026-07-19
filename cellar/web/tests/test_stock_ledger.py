"""
Regression tests for the consumable stock ledger (migration 0028).

THE BUG THIS PINS DOWN
----------------------
`Addition.cost` read `additive.unit_cost` LIVE. Repricing an additive restated the
COGS of every lot that had ever used it — the same defect 0027 fixed for blends.
test_reprice_does_not_restate_history is the one that failed before this tranche.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from cellar.models import (
    Addition, Additive, DryGood, Lot, PhysicalCount, StockTransaction, Variety,
)
from cellar.models.base import Program
from cellar.services import generator, stock as stock_svc

# settings.SECURE_SSL_REDIRECT is on, so a plain-http POST 301s and the follow
# silently degrades it to a GET. Disable it here, and skip the hashed-static
# manifest the sandbox has no collectstatic output for.
WEB = override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})


class UnitConversionTests(TestCase):
    def test_mass_conversions_are_exact(self):
        self.assertEqual(stock_svc.convert(1, "kg", "g"), Decimal("1000"))
        self.assertEqual(stock_svc.convert(1, "lb", "g"), Decimal("453.59237"))
        self.assertEqual(stock_svc.convert(4, "kg", "g"), Decimal("4000"))

    def test_identical_and_unknown_but_equal_units_pass_through(self):
        self.assertEqual(stock_svc.convert(7, "g", "g"), Decimal("7"))
        self.assertEqual(stock_svc.convert(7, "widget", "widget"), Decimal("7"))

    def test_cross_dimension_conversion_raises(self):
        """A silent 1:1 here would be a 453x costing error. It must fail loudly."""
        with self.assertRaises(stock_svc.UnitMismatch):
            stock_svc.convert(1, "kg", "L")


class WeightedAverageTests(TestCase):
    def setUp(self):
        self.item = Additive.objects.create(name="TEST Opti-Red", category=Additive.Category.NUTRIENT,
                                            unit="g")

    def test_receipt_rolls_freight_and_tax_into_landed_cost(self):
        txn = stock_svc.receive(self.item, pack_count=4, pack_size=1, pack_unit="kg",
                                goods_cost=160, freight_cost=20, tax_cost=8)
        self.assertEqual(txn.quantity, Decimal("4000.0000"))       # 4 kg -> 4,000 g
        self.assertEqual(txn.landed_cost, Decimal("188.00"))
        self.assertEqual(txn.extended_cost, Decimal("188.00"))
        self.assertEqual(stock_svc.wac(self.item), Decimal("0.047000"))

    def test_second_receipt_moves_the_average(self):
        stock_svc.receive(self.item, pack_count=100, pack_size=1, pack_unit="g", goods_cost=1000)
        self.assertEqual(stock_svc.wac(self.item), Decimal("10.000000"))
        stock_svc.receive(self.item, pack_count=50, pack_size=1, pack_unit="g", goods_cost=600)
        # (1000 + 600) / 150 = 10.6667
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("150.0000"))
        self.assertEqual(stock_svc.wac(self.item), Decimal("10.666667"))

    def test_issue_draws_at_current_average_and_reduces_value(self):
        stock_svc.receive(self.item, pack_count=1, pack_size=1, pack_unit="kg", goods_cost=100)
        stock_svc.issue(self.item, 250)                # 250 g of 1,000
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("750.0000"))
        self.assertEqual(stock_svc.on_hand_value(self.item), Decimal("75.00"))
        self.assertEqual(stock_svc.wac(self.item), Decimal("0.100000"))   # unchanged

    def test_negative_on_hand_is_allowed(self):
        stock_svc.receive(self.item, pack_count=100, pack_size=1, pack_unit="g", goods_cost=100)
        stock_svc.issue(self.item, 150)
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("-50.0000"))

    def test_write_down_expenses_stock(self):
        stock_svc.receive(self.item, pack_count=100, pack_size=1, pack_unit="g", goods_cost=100)
        txn = stock_svc.write_down(self.item, 10, reason="expired")
        self.assertEqual(txn.extended_cost, Decimal("-10.00"))
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("90.0000"))

    def test_write_down_requires_a_reason(self):
        with self.assertRaises(ValueError):
            stock_svc.write_down(self.item, 5, reason="  ")

    def test_exactly_one_item_fk_is_enforced(self):
        from django.db.utils import IntegrityError
        dg = DryGood.objects.create(name="TEST Cork", kind=DryGood.Kind.CLOSURE,
                                    unit_cost=Decimal("0.35"))
        with self.assertRaises(IntegrityError):
            StockTransaction.objects.create(
                kind=StockTransaction.Kind.RECEIPT, occurred_at=timezone.localdate(),
                additive=self.item, dry_good=dg, quantity=Decimal("1"))


class AdditionCostSnapshotTests(TestCase):
    def setUp(self):
        self.v = Variety.objects.create(name="Zinfandel")
        self.lot = generator.create_lot(2025, self.v, Program.TABLE)
        self.item = Additive.objects.create(name="TEST Tartaric", category=Additive.Category.ACID,
                                            unit="g", unit_cost=Decimal("0.01"))

    def _add(self, grams):
        return Addition.objects.create(lot=self.lot, additive=self.item, target="test",
                                       quantity=Decimal(str(grams)), added_at=timezone.now())

    def test_addition_draws_stock_and_snapshots_cost(self):
        stock_svc.receive(self.item, pack_count=1, pack_size=1, pack_unit="kg", goods_cost=50)
        a = self._add(200)                              # $0.05/g
        self.assertEqual(a.unit_cost_snapshot, Decimal("0.050000"))
        self.assertEqual(a.cost, Decimal("10.000000"))
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("800.0000"))
        self.assertEqual(a.stock_txns.count(), 1)

    def test_reprice_does_not_restate_history(self):
        """THE REGRESSION. A later, dearer receipt must not move an old addition."""
        stock_svc.receive(self.item, pack_count=1000, pack_size=1, pack_unit="g", goods_cost=10)
        first = self._add(100)                          # $0.01/g -> $1.00
        self.assertEqual(first.cost, Decimal("1.000000"))

        stock_svc.receive(self.item, pack_count=1000, pack_size=1, pack_unit="g", goods_cost=90)
        first.refresh_from_db()
        self.assertEqual(first.cost, Decimal("1.000000"))          # unmoved

        second = self._add(100)
        self.assertGreater(second.cost, first.cost)                 # new adds pay the new rate

    def test_untracked_additive_draws_no_stock(self):
        """Water is dosed but never purchased — it must not issue."""
        water = Additive.objects.create(name="TEST Water", category=Additive.Category.OTHER,
                                        unit="gal", dose_mode=Additive.DoseMode.PCT_VOLUME,
                                        track_stock=False)
        a = Addition.objects.create(lot=self.lot, additive=water, target="10%",
                                    quantity=Decimal("87"), added_at=timezone.now())
        self.assertEqual(a.stock_txns.count(), 0)

    def test_issue_is_idempotent_on_resave(self):
        stock_svc.receive(self.item, pack_count=1000, pack_size=1, pack_unit="g", goods_cost=10)
        a = self._add(100)
        a.save()                                        # AppendOnly allows a no-change save
        self.assertEqual(a.stock_txns.count(), 1)
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("900.0000"))


class PhysicalCountTests(TestCase):
    def setUp(self):
        self.item = Additive.objects.create(name="TEST DAP", category=Additive.Category.NUTRIENT,
                                            unit="g")
        stock_svc.receive(self.item, pack_count=1000, pack_size=1, pack_unit="g", goods_cost=100)

    def test_short_count_books_negative_variance(self):
        c = PhysicalCount.objects.create(counted_on=timezone.localdate(), label="FY25")
        stock_svc.commit_count(c, [(self.item, Decimal("940"))])
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("940.0000"))
        self.assertEqual(c.variance_value, Decimal("-6.00"))       # 60 g @ $0.10
        self.assertTrue(c.is_committed)

    def test_count_at_book_writes_no_row(self):
        c = PhysicalCount.objects.create(counted_on=timezone.localdate())
        written = stock_svc.commit_count(c, [(self.item, Decimal("1000"))])
        self.assertEqual(written, [])
        self.assertEqual(c.adjustments.count(), 0)

    def test_count_cannot_be_committed_twice(self):
        c = PhysicalCount.objects.create(counted_on=timezone.localdate())
        stock_svc.commit_count(c, [(self.item, Decimal("900"))])
        with self.assertRaises(ValueError):
            stock_svc.commit_count(c, [(self.item, Decimal("800"))])

    def test_variance_never_touches_a_lot(self):
        """Shrinkage is period expense — no LotCostAdjustment, no lot FK anywhere."""
        from cellar.models import LotCostAdjustment
        c = PhysicalCount.objects.create(counted_on=timezone.localdate())
        stock_svc.commit_count(c, [(self.item, Decimal("500"))])
        self.assertEqual(LotCostAdjustment.objects.count(), 0)


@WEB
class InventoryPageTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.c = Client()
        self.c.force_login(U.objects.create_user("t", password="x"))
        self.item = Additive.objects.create(name="TEST Opti-Red", category=Additive.Category.NUTRIENT,
                                            unit="g")

    def test_pages_render(self):
        stock_svc.receive(self.item, pack_count=1, pack_size=1, pack_unit="kg", goods_cost=100)
        for name, args in [("stock-index", []), ("stock-receive", []),
                           ("stock-write-down", []), ("stock-counts", [])]:
            r = self.c.get(reverse(name, args=args), follow=True)
            self.assertEqual(r.status_code, 200, name)
        r = self.c.get(reverse("stock-item", args=["additive", self.item.pk]), follow=True)
        self.assertEqual(r.status_code, 200)

    def test_receive_via_form(self):
        r = self.c.post(reverse("stock-receive"), {
            "item": f"additive:{self.item.pk}", "pack_count": "4", "pack_size": "1",
            "pack_unit": "kg", "goods_cost": "160", "freight_cost": "20", "tax_cost": "8",
            "occurred_at": timezone.localdate().isoformat(), "supplier": "Scott Labs",
            "reference": "PO-1001"}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("4000.0000"))
        self.assertEqual(stock_svc.wac(self.item), Decimal("0.047000"))

    def test_bad_unit_shows_an_error_not_a_500(self):
        r = self.c.post(reverse("stock-receive"), {
            "item": f"additive:{self.item.pk}", "pack_count": "1", "pack_size": "1",
            "pack_unit": "L", "goods_cost": "10"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "different kinds of measurement", status_code=400)

    def test_count_sheet_commits(self):
        stock_svc.receive(self.item, pack_count=1000, pack_size=1, pack_unit="g", goods_cost=100)
        c = PhysicalCount.objects.create(counted_on=timezone.localdate())
        r = self.c.post(reverse("stock-count", args=[c.pk]),
                        {f"qty:additive:{self.item.pk}": "940"}, follow=True)
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertTrue(c.is_committed)
        self.assertEqual(stock_svc.on_hand(self.item), Decimal("940.0000"))
