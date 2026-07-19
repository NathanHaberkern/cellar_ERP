"""
Consumable stock ledger — perpetual weighted-average inventory for additives,
dry goods, and Part IV materials.

WHY THIS EXISTS
---------------
Before this module, `Additive.unit_cost` / `DryGood.unit_cost` / `Material.unit_cost`
were single mutable numbers. `Addition.cost` read the additive's unit_cost LIVE, so
repricing a bag of tartaric silently restated the COGS of every lot that had ever
used it — the same defect the LotLineage snapshot (0027) fixed for blends.

The fix is the pattern HighProofSpiritLedger already proves out for spirits:
receipts carry a total landed cost, on-hand quantity and value are sums over an
append-only ledger, and consumption snapshots the weighted average at the moment
it happens. History never moves again.

THREE CATALOGS, ONE LEDGER
--------------------------
Additive, DryGood and Material stay as they are — they're the item catalogs, and
each is referenced by existing FKs (Addition.additive, BottlingDryGoodUse.dry_good,
MaterialTransaction.material) that would be expensive and pointless to migrate.
StockTransaction carries one nullable FK to each and a CheckConstraint that exactly
one is set.

Chosen over a GenericForeignKey deliberately: real FKs keep on_delete=PROTECT (you
cannot delete an additive out from under its purchase history), keep joins readable,
and are far easier to explain in an audit than a content_type/object_id pair. Adding
a fourth catalog later is one nullable column plus a constraint edit.

SIGN CONVENTION
---------------
`quantity` and `extended_cost` are SIGNED and always in the item's own stock unit:

    RECEIPT           + qty   + dollars
    ISSUE             − qty   − dollars     (an addition / a bottling dry-good use)
    WRITE_DOWN        − qty   − dollars     (expired, spilled, damaged)
    COUNT_ADJUSTMENT  ± qty   ± dollars     (book reconciled to a physical count)

On-hand quantity is therefore Sum(quantity) and on-hand value is Sum(extended_cost)
over non-void rows — no branching, and a voided row drops out of both at once.

WHAT IS NOT CAPITALIZED
-----------------------
COUNT_ADJUSTMENT and WRITE_DOWN dollars are period expense (shrinkage), never
pushed back onto a lot's COGS. Nate's call, and it's the standard treatment: you
do not recost finished wine because a bag of nutrient came up light in November.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum

from .base import AppendOnly

QTY = Decimal("0.0001")
MONEY = Decimal("0.01")


class PhysicalCount(AppendOnly):
    """A counting session. Lines are the COUNT_ADJUSTMENT transactions pointing here.

    Draft until committed: a count is entered over an afternoon, and nothing should
    hit the ledger until the whole sheet is keyed. `committed_at` is in CLOSE_FIELDS
    so it can be stamped once, after creation, without breaking append-only.
    """
    counted_on = models.DateField()
    label = models.CharField(max_length=120, blank=True,
                             help_text="e.g. 'FY25 year-end', 'post-bottling spot check'")
    committed_at = models.DateTimeField(null=True, blank=True)

    CLOSE_FIELDS = ("committed_at",)

    class Meta:
        ordering = ("-counted_on", "-id")

    @property
    def is_committed(self):
        return self.committed_at is not None

    @property
    def variance_value(self):
        """Net dollar shrinkage this count booked (negative = wrote inventory down)."""
        return self.adjustments.filter(voided_at__isnull=True).aggregate(
            v=Sum("extended_cost"))["v"] or Decimal("0")

    def __str__(self):
        return f"{self.label or 'Count'} {self.counted_on}"


class StockTransaction(AppendOnly):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", "Receipt"
        ISSUE = "issue", "Issued to production"
        COUNT_ADJUSTMENT = "count_adj", "Count adjustment"
        WRITE_DOWN = "write_down", "Write-down / disposal"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    occurred_at = models.DateField()

    # --- exactly one of these three (enforced by CheckConstraint below) ---
    additive = models.ForeignKey("cellar.Additive", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="stock_txns")
    dry_good = models.ForeignKey("cellar.DryGood", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="stock_txns")
    material = models.ForeignKey("cellar.Material", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="stock_txns")

    quantity = models.DecimalField(max_digits=14, decimal_places=4,
                                   help_text="signed, in the item's stock unit")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True,
                                    help_text="receipt: landed ÷ qty. issue: WAC at issue.")
    extended_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0,
                                        help_text="signed dollars = quantity × unit_cost")

    # --- receipt-only detail -------------------------------------------------
    # Freight and tax are LANDED COST: they roll into unit_cost and therefore into
    # the weighted average, but stay visible as their own numbers so an invoice can
    # be tied out line by line. Same treatment BarrelOrder gives delivery_fee/bank_fee.
    supplier = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=60, blank=True, help_text="PO / invoice no.")
    goods_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    freight_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pack_count = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                     help_text="how many packs, e.g. 4 bags")
    pack_size = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True,
                                    help_text="size of one pack, e.g. 1 (kg)")
    pack_unit = models.CharField(max_length=20, blank=True,
                                 help_text="purchase unit, e.g. kg — converted to the item's stock unit")

    # --- provenance ----------------------------------------------------------
    count = models.ForeignKey(PhysicalCount, null=True, blank=True,
                              on_delete=models.PROTECT, related_name="adjustments")
    addition = models.ForeignKey("cellar.Addition", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="stock_txns")
    dry_good_use = models.ForeignKey("cellar.BottlingDryGoodUse", null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="stock_txns")
    reason = models.CharField(max_length=160, blank=True,
                              help_text="write-down reason: expired, spilled, damaged…")

    class Meta:
        ordering = ("occurred_at", "id")
        constraints = [
            models.CheckConstraint(
                name="stocktxn_exactly_one_item",
                condition=(
                    Q(additive__isnull=False, dry_good__isnull=True, material__isnull=True)
                    | Q(additive__isnull=True, dry_good__isnull=False, material__isnull=True)
                    | Q(additive__isnull=True, dry_good__isnull=True, material__isnull=False)
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["additive", "occurred_at"]),
            models.Index(fields=["dry_good", "occurred_at"]),
            models.Index(fields=["material", "occurred_at"]),
        ]

    # ------------------------------------------------------------------ item
    @property
    def item(self):
        return self.additive or self.dry_good or self.material

    @property
    def item_kind(self):
        if self.additive_id:
            return "additive"
        if self.dry_good_id:
            return "dry_good"
        return "material"

    @property
    def landed_cost(self):
        """Receipt total actually capitalized: goods + freight + tax."""
        if self.kind != self.Kind.RECEIPT:
            return None
        return ((self.goods_cost or Decimal("0"))
                + (self.freight_cost or Decimal("0"))
                + (self.tax_cost or Decimal("0")))

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.kind == self.Kind.RECEIPT and self.goods_cost is not None:
                landed = self.landed_cost
                if self.quantity:
                    self.unit_cost = (landed / self.quantity).quantize(Decimal("0.000001"))
                self.extended_cost = landed.quantize(MONEY)
            elif self.extended_cost in (None, 0) and self.unit_cost is not None:
                self.extended_cost = (self.quantity * self.unit_cost).quantize(MONEY)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_kind_display()} {self.quantity} × {self.item} ({self.occurred_at})"
