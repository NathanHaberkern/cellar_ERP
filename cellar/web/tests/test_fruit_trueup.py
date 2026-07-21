"""
Regression tests for the fruit price true-up (migration 0031).

The scenario throughout is the real one. The Grape Crush Report for a vintage does
not publish until February (preliminary) and March (final) of the FOLLOWING year,
so fruit received in September can only be priced against the prior year's district
average. That price is provisional. When the final figure lands the following March
the difference has to reach the lot without restating what was already booked.

Three behaviours carry the design:

  * test_as_booked_price_is_never_rewritten — the reason FruitPriceRevision is a
    separate row instead of an edit to FruitPrice. Repricing the source would move
    the cost of every lot in the vintage, including lots posted into closed months.
  * test_ledger_still_reconciles_after_trueup — repricing the source would also
    move `computed` while `posted` stayed put, so every lot in the vintage would
    report as drifted and close_period() would refuse. The additive true-up keeps
    both sides in step.
  * test_invoice_priced_fruit_is_exempt — a true-up may only touch allocations that
    actually resolved to a FruitPrice row. A tag carrying its own
    `fruit_cost_per_ton` short-circuits before FruitPrice is consulted, and revising
    the varietal price must not move it.
"""
from datetime import date
from decimal import Decimal

from django.db.utils import IntegrityError
from django.test import TestCase

from cellar.models import (
    Block, CostEntry, FruitPrice, FruitPriceRevision, Grower, HarvestEvent, Lot,
    Variety, Vineyard, WeighTag, WeighTagAllocation,
)
from cellar.services import cost_ledger, costing

PROVISIONAL = Decimal("1600.00")     # 2025 district average, booked Sept 2026
FINAL = Decimal("1425.00")           # 2026 final, published March 2027 — market fell
TONS = 20


class _Base(TestCase):
    def setUp(self):
        self.variety = Variety.objects.create(name="TEST Zinfandel")
        grower = Grower.objects.create(name="TEST Mohr-Fry", source_type="purchased")
        vy = Vineyard.objects.create(grower=grower, name="TEST Home", crush_district=11)
        self.block = Block.objects.create(vineyard=vy, variety=self.variety,
                                          name="TEST Marian's", acreage=Decimal("12.0"))
        self.harvest = HarvestEvent.objects.create(block=self.block,
                                                   harvest_date=date(2026, 9, 18))
        self.price = FruitPrice.objects.create(
            vintage_year=26, variety=self.variety, block=None,
            price_per_ton=PROVISIONAL,
            basis=FruitPrice.Basis.PRIOR_YEAR_DISTRICT,
            source_ref="Grape Crush Report 2025 Final, District 11, Zinfandel",
            is_provisional=True)
        self.lot = self._lot("T-1001", TONS)

    _n = 0

    def _lot(self, tag_no, tons, cost_per_ton=None):
        type(self)._n += 1
        lot = Lot.objects.create(vintage_year=26)
        tag = WeighTag.objects.create(
            harvest_event=self.harvest, weigh_tag_number=tag_no,
            net_weight_lbs=Decimal(tons * 2000), source_type="purchased",
            disposition="crushed", fruit_cost_per_ton=cost_per_ton)
        WeighTagAllocation.objects.create(lot=lot, weigh_tag=tag,
                                          allocated_net_lbs=Decimal(tons * 2000))
        return lot

    def _revise(self, final=FINAL, on=date(2027, 3, 10)):
        return FruitPriceRevision.objects.create(
            price=self.price, final_price_per_ton=final,
            basis=FruitPrice.Basis.DISTRICT_AVERAGE,
            source_ref="Grape Crush Report 2026 Final, District 11, Zinfandel",
            effective_on=on)


class FruitTrueUpDerivationTests(_Base):

    def test_no_trueup_before_a_revision_exists(self):
        self.assertAlmostEqual(costing.fruit_cost(self.lot), 32000.0, places=2)
        self.assertAlmostEqual(costing.fruit_trueup_cost(self.lot), 0.0, places=2)

    def test_as_booked_price_is_never_rewritten(self):
        self._revise()
        self.price.refresh_from_db()
        self.assertEqual(self.price.price_per_ton, PROVISIONAL)
        self.assertEqual(self.price.final_price_per_ton, FINAL)
        self.assertEqual(self.price.trueup_delta_per_ton, Decimal("-175.00"))
        # the delivery-date fruit line is untouched; only the new term moves
        self.assertAlmostEqual(costing.fruit_cost(self.lot), 32000.0, places=2)

    def test_falling_market_trues_up_as_a_credit(self):
        self._revise()
        self.assertAlmostEqual(costing.fruit_trueup_cost(self.lot), -3500.0, places=2)
        self.assertAlmostEqual(costing.lot_direct_cost(self.lot), 28500.0, places=2)

    def test_rising_market_trues_up_as_a_charge(self):
        self._revise(final=Decimal("1750.00"))
        self.assertAlmostEqual(costing.fruit_trueup_cost(self.lot), 3000.0, places=2)

    def test_invoice_priced_fruit_is_exempt(self):
        """A tag with its own cost/ton never consulted FruitPrice, so nothing to true up."""
        lot2 = self._lot("T-1002", 10, cost_per_ton=Decimal("2100.00"))
        self._revise()
        self.assertAlmostEqual(costing.fruit_cost(lot2), 21000.0, places=2)
        self.assertAlmostEqual(costing.fruit_trueup_cost(lot2), 0.0, places=2)

    def test_only_one_live_revision_per_price(self):
        self._revise()
        with self.assertRaises(IntegrityError):
            self._revise(final=Decimal("1500.00"), on=date(2027, 4, 1))


class FruitTrueUpPostingTests(_Base):

    def test_posts_as_fruit_dated_to_publication(self):
        cost_ledger.post_lot(self.lot)
        self._revise()
        cost_ledger.post_lot(self.lot)

        row = CostEntry.objects.get(source_kind="weightagallocation_trueup",
                                    voided_at__isnull=True)
        self.assertEqual(row.category, CostEntry.Category.FRUIT)
        self.assertEqual(row.amount, Decimal("-3500.00"))
        # dated to the day the report published, not the September delivery
        self.assertEqual(row.occurred_at, date(2027, 3, 10))
        # and the original fruit posting is still there, untouched
        base = CostEntry.objects.get(source_kind="weightagallocation",
                                     voided_at__isnull=True)
        self.assertEqual(base.amount, Decimal("32000.00"))

    def test_posting_is_idempotent(self):
        cost_ledger.post_lot(self.lot)
        self._revise()
        for _ in range(3):
            cost_ledger.post_lot(self.lot)
        self.assertEqual(
            CostEntry.objects.filter(voided_at__isnull=True).count(), 2)
        self.assertAlmostEqual(cost_ledger.lot_cost_posted(self.lot), 28500.0, places=2)

    def test_ledger_still_reconciles_after_trueup(self):
        cost_ledger.post_lot(self.lot)
        self.assertTrue(all(r["ok"] for r in cost_ledger.reconcile()))
        self._revise()
        cost_ledger.post_lot(self.lot)
        self.assertTrue(all(r["ok"] for r in cost_ledger.reconcile()))

    def test_unposted_trueup_shows_as_drift(self):
        """The safety net: a revision entered but not yet posted must fail recon,
        so close_period() refuses rather than closing a month on a stale number."""
        cost_ledger.post_lot(self.lot)
        self._revise()
        bad = [r for r in cost_ledger.reconcile() if not r["ok"]]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["diff"], Decimal("3500.00"))
