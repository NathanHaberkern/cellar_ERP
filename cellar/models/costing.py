"""
Manual per-lot cost adjustments.

WHY THIS EXISTS
---------------
`costing.lot_direct_cost()` builds a lot's cost from things the cellar actually
recorded: weigh-tag allocations (fruit), Addition rows (additives), fortification
draws (spirit), and AgingPlacement custody intervals (barrel depreciation).

That works for a vintage recorded live. It does NOT work for a vintage imported
from paper, where the barrel placements were never captured and the additions
were never keyed — barrel depreciation silently comes back $0 and the additive
line is empty, so the lot looks cheaper than it was.

This model is the escape hatch: a dated, signed, append-only dollar amount booked
against a lot, with a `kind` saying what it stands in for. It is deliberately NOT
derived from anything — somebody typed it, and the row records who and when, so an
auditor can tell a measured cost from an assigned one.

Used by the historical importer for oak (per-lot, entered by hand) and overhead
(a pool allocated across lots by volume), but it is not import-only: a 2025 lot
that picks up a real cost with no home in the ledger can carry one too.
"""
from django.db import models

from .base import AppendOnly


class LotCostAdjustment(AppendOnly):
    """A manually-assigned dollar cost on a lot. Never derived; always typed."""

    class Kind(models.TextChoices):
        OAK = "oak", "Oak / barrel"
        OVERHEAD = "overhead", "Cellar overhead"
        LABOR = "labor", "Labor"
        ADDITIVES = "additives", "Additives (not itemized)"
        OTHER = "other", "Other"

    class Basis(models.TextChoices):
        ENTERED = "entered", "Entered per lot"
        ALLOCATED = "allocated", "Allocated from a pool"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT,
                            related_name="cost_adjustments")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="signed dollars; negative to credit a lot")
    incurred_at = models.DateField()
    basis = models.CharField(max_length=10, choices=Basis.choices, default=Basis.ENTERED,
                             help_text="entered per lot, or allocated from a pool")

    class Meta:
        ordering = ("incurred_at", "id")

    def __str__(self):
        return f"{self.lot} {self.get_kind_display()} ${self.amount} ({self.incurred_at})"


# =============================================================================
# Cost ledger — the posted, period-locked record of what wine cost.
# =============================================================================
#
# WHY A STORED LEDGER, WHEN compliance_ledger.py IS DERIVED
# ---------------------------------------------------------
# The compliance ledger rebuilds its rows from source models on every read and so
# reconciles to volumes.lot_balance() by construction — it cannot drift, and it
# never needs to. The cost ledger cannot work that way, for one reason: a closed
# accounting period has to STAY closed. Once March is summarised into a QBO journal
# entry, re-deriving March from live objects would silently restate a number that
# has already left the building. There is nothing to lock in a derived view.
#
# So costs are POSTED here, once, and the posted row is the record. The price of
# breaking the house idiom is that these rows can drift from their sources, which
# is why cost_ledger.reconcile() exists and why it should be run before every close:
# it re-derives each lot's cost live and diffs it against what was posted.
#
# WHY source_kind/source_id AND NOT REAL FOREIGN KEYS
# ---------------------------------------------------
# A posting references six-plus source models (WeighTagAllocation, Addition,
# FortificationEvent, AgingPlacement, LotCostAdjustment, BottlingDryGoodUse,
# StockTransaction…). Real FKs would mean six nullable columns and, worse,
# on_delete=PROTECT chains that block voiding a source row that has been posted —
# exactly backwards, since voiding a source is how you correct a mistake. The soft
# reference keeps the audit trail without the coupling, and the unique constraint
# below gives idempotency: posting is safe to re-run any number of times.


class CostPeriod(models.Model):
    """One accounting month. Closing it freezes what has been posted into it."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        POSTED = "posted", "Posted to QBO"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    qbo_journal_entry_id = models.CharField(max_length=60, blank=True,
                                            help_text="set when the summary JE lands in QBO")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-year", "-month")
        constraints = [models.UniqueConstraint(fields=["year", "month"],
                                               name="costperiod_unique_year_month")]

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    @property
    def label(self):
        return f"{self.year}-{self.month:02d}"

    def __str__(self):
        return f"{self.label} ({self.get_status_display()})"


class CostEntry(AppendOnly):
    """One posted dollar amount. Signed. Sums to a lot's cost, or to a period's expense."""

    class Category(models.TextChoices):
        FRUIT = "fruit", "Fruit"
        ADDITIVE = "additive", "Additives"
        SPIRIT = "spirit", "Spirit (HPGS)"
        OAK = "oak", "Oak / barrel depreciation"
        PACKAGING = "packaging", "Packaging / dry goods"
        LABOR = "labor", "Labor"
        OVERHEAD = "overhead", "Cellar overhead"
        ADJUSTMENT = "adjustment", "Manual adjustment"
        # Cost moving between lots. A blend posts BOTH: a negative TRANSFER_OUT on
        # the parent and a positive TRANSFER_IN on the child, at the LotLineage
        # snapshot rate. This is what finally fixes the defect flagged in the first
        # costing review — under the old live computation a parent's cost was never
        # reduced by wine it gave away, so a blend inflated total inventory value.
        TRANSFER_IN = "transfer_in", "Cost transferred in"
        TRANSFER_OUT = "transfer_out", "Cost transferred out"
        # Period expense, never capitalized into a lot (lot stays null).
        SHRINKAGE = "shrinkage", "Inventory shrinkage"
        ABNORMAL_LOSS = "abnormal_loss", "Abnormal loss"
        IDLE_CAPACITY = "idle_capacity", "Idle capacity"

    #: categories that are period expense — these carry lot=None by design
    EXPENSE_CATEGORIES = ("shrinkage", "abnormal_loss", "idle_capacity")

    lot = models.ForeignKey("cellar.Lot", null=True, blank=True, on_delete=models.PROTECT,
                            related_name="cost_entries",
                            help_text="null for period expense with no wine to attach to")
    period = models.ForeignKey(CostPeriod, on_delete=models.PROTECT, related_name="entries")
    category = models.CharField(max_length=16, choices=Category.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2,
                                 help_text="signed dollars")
    occurred_at = models.DateField(help_text="business date of the underlying event")

    source_kind = models.CharField(max_length=32, blank=True)
    source_id = models.PositiveIntegerField(null=True, blank=True)

    deferred_note = models.CharField(
        max_length=200, blank=True,
        help_text="set when the event's own month was already closed and the cost "
                  "was posted forward instead — mandatory, so a shifted cost is never silent")

    class Meta:
        ordering = ("occurred_at", "id")
        constraints = [
            # Idempotency: one live posting per (source, category). Re-running the
            # poster is a no-op rather than a double-count. Voided rows are excluded
            # so a corrected posting can be re-made.
            models.UniqueConstraint(
                fields=["source_kind", "source_id", "category"],
                condition=models.Q(voided_at__isnull=True, source_id__isnull=False),
                name="costentry_one_live_posting_per_source"),
        ]
        indexes = [
            models.Index(fields=["lot", "category"]),
            models.Index(fields=["period", "category"]),
        ]

    @property
    def is_expense(self):
        return self.category in self.EXPENSE_CATEGORIES

    def __str__(self):
        where = self.lot.code if self.lot_id else "period expense"
        return f"{where} {self.get_category_display()} ${self.amount} ({self.occurred_at})"


# =============================================================================
# Overhead pools
# =============================================================================
#
# Six pools rather than one bucket, so a cost/gal that moves can be traced to the
# thing that moved it. Each pool gets a dollar amount per month, entered by hand,
# and the allocation engine spreads it over bulk gallons.
#
# WHAT ABSORBS, AND WHAT DOESN'T
# ------------------------------
#   * Bulk wine only. Case goods have left WIP; bottling costs go direct to the run.
#   * Lots older than `overhead_absorption_max_years` (default 3) drop OUT of the
#     denominator. A 2014 Port would otherwise absorb a slice of every monthly pool
#     for 144 months and end up carried above what it can be sold for, which forces
#     an NRV write-down nobody wants. Capping is simpler and more conservative.
#   * When actual gallons fall below `normal_capacity_gal`, the unabsorbed share is
#     EXPENSED as idle capacity rather than loaded onto whatever wine happens to
#     exist. A light vintage should not make its own wine look expensive. This is
#     the ASC 330 treatment, and `barrel_depreciation_by_lot()` already does the
#     same thing with its `overhead` bucket for empty barrel-years.


class OverheadPool(models.Model):
    """Master data: the named buckets overhead is entered into."""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    category = models.CharField(
        max_length=16, default="overhead",
        help_text="CostEntry category this pool posts as — 'labor' or 'overhead'")
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("sort_order", "name")

    def __str__(self):
        return self.name


class OverheadPoolPeriod(AppendOnly):
    """One pool's dollars for one month, plus what happened when it was allocated."""

    pool = models.ForeignKey(OverheadPool, on_delete=models.PROTECT, related_name="periods")
    period = models.ForeignKey(CostPeriod, on_delete=models.PROTECT, related_name="pool_amounts")
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="total dollars in this pool for this month")
    allocated_at = models.DateTimeField(null=True, blank=True)
    absorbed_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    idle_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    absorbing_gallons = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)

    CLOSE_FIELDS = ("allocated_at", "absorbed_amount", "idle_amount", "absorbing_gallons")

    class Meta:
        ordering = ("period", "pool")
        constraints = [
            models.UniqueConstraint(
                fields=["pool", "period"], condition=models.Q(voided_at__isnull=True),
                name="overheadpoolperiod_one_live_per_pool_month"),
        ]

    @property
    def is_allocated(self):
        return self.allocated_at is not None

    def __str__(self):
        return f"{self.pool} {self.period.label} ${self.amount}"
