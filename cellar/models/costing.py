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
