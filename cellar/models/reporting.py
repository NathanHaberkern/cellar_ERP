"""
Reporting-tranche capture events — the operations the 2025 filings revealed that
weren't yet modeled. These feed the 5120.17 read-layer in services/reporting.py.

  * BondTransfer     — sales/movements to (or from) another bonded premises  (A15/A7, B9/B3)
  * SweeteningEvent  — Vino Blanc back-sweetening with concentrate           (A3/A18, Part IV)
  * Material / MaterialTransaction — non-grape materials (concentrate, sugar) for Part IV
  * BondAdjustment   — the minor movements: tasting, family use, taxpaid-return,
                       inventory loss/gain, breakage, dump-to-bulk            (misc lines)
"""
from decimal import Decimal

from django.db import models
from .base import AppendOnly
from .crushout import TaxClass


class Phase(models.TextChoices):
    BULK = "bulk", "Bulk (Section A)"
    BOTTLED = "bottled", "Bottled (Section B)"


class ExternalDestination(models.Model):
    """A buyer or receiving bonded premises for wine/juice/grapes leaving the
    winery — reference data (editable master), not a ledger row. The BW number
    is what a B2B in-bond transfer's paperwork needs; leave it blank for a
    plain taxpaid customer."""
    name = models.CharField(max_length=120, unique=True)
    bw_number = models.CharField(max_length=30, blank=True,
        help_text="e.g. BW-CA-1234 — required for in-bond (not-yet-taxpaid) transfers")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.bw_number})" if self.bw_number else self.name


class BondTransfer(AppendOnly):
    """Wine moved in bond to/from another bonded wine premises."""
    class Direction(models.TextChoices):
        OUT = "out", "Transferred out"
        IN = "in", "Received in bond"

    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.OUT)
    phase = models.CharField(max_length=8, choices=Phase.choices, default=Phase.BULK)
    tax_class = models.CharField(max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16)
    gallons = models.DecimalField(max_digits=10, decimal_places=1)
    transferred_at = models.DateField()
    counterparty = models.CharField(max_length=120, blank=True, help_text="other winery")
    destination = models.ForeignKey(ExternalDestination, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+",
                                    help_text="reference-table pick; counterparty stays the free-text display")
    lot = models.ForeignKey("cellar.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"{self.get_direction_display()} {self.gallons} gal {self.phase} {self.transferred_at}"


class Material(models.Model):
    class Kind(models.TextChoices):
        CONCENTRATE = "concentrate", "Concentrate"
        SUGAR = "sugar", "Sugar"
        JUICE = "juice", "Juice"

    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    unit = models.CharField(max_length=20, default="gal")
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    def __str__(self):
        return self.name


class MaterialTransaction(AppendOnly):
    """Received / used / destroyed, for Part IV materials tracking."""
    class Direction(models.TextChoices):
        RECEIVED = "received", "Received"
        USED = "used", "Used in production"
        DESTROYED = "destroyed", "Destroyed / discarded"

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="transactions")
    direction = models.CharField(max_length=12, choices=Direction.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    occurred_at = models.DateField()

    def __str__(self):
        return f"{self.get_direction_display()} {self.quantity} {self.material}"


class SweeteningEvent(AppendOnly):
    """Back-sweetening: wine used (line 18) + concentrate → sweetened wine produced (line 3).
    produced = used + concentrate. Concentrate also books a Part IV material use."""
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="sweetenings")
    sweetened_at = models.DateField()
    tax_class = models.CharField(max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16)
    volume_used = models.DecimalField(max_digits=10, decimal_places=1, help_text="wine before sweetening (line 18)")
    concentrate = models.ForeignKey(Material, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    concentrate_gallons = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    brix_before = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    brix_after = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    material_use = models.ForeignKey(MaterialTransaction, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="+")

    @property
    def volume_produced(self):
        if self.volume_used is None:
            return None
        return (self.volume_used + (self.concentrate_gallons or 0)).quantize(Decimal("0.1"))

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if creating and self.concentrate_id and self.concentrate_gallons and self.material_use_id is None:
            self.material_use = MaterialTransaction.objects.create(
                material=self.concentrate, direction=MaterialTransaction.Direction.USED,
                quantity=self.concentrate_gallons, occurred_at=self.sweetened_at)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.lot} sweetened {self.sweetened_at}"


class BulkTaxPaidRemoval(AppendOnly):
    """Bulk wine removed taxpaid (5120.17 line A14) — sold in bulk, not bottled."""
    class Channel(models.TextChoices):
        DTC = "dtc", "Direct-to-consumer"
        WHOLESALE = "wholesale", "Wholesale"
        OTHER = "other", "Other"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="bulk_removals")
    tax_class = models.CharField(max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16)
    wine_gallons = models.DecimalField(max_digits=10, decimal_places=1)
    removed_at = models.DateField()
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.WHOLESALE)
    destination = models.ForeignKey(ExternalDestination, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"bulk taxpaid {self.wine_gallons} gal ({self.tax_class}) {self.removed_at}"


class BondAdjustment(AppendOnly):
    """The minor 5120.17 movements captured simply for now."""
    class Kind(models.TextChoices):
        TASTING = "tasting", "Used for tasting (B11)"
        FAMILY_USE = "family_use", "Removed for family use (B13)"
        TAXPAID_RETURN = "taxpaid_return", "Taxpaid wine returned to bond (B4)"
        INVENTORY_LOSS = "inventory_loss", "Inventory loss (A30 / B19)"
        INVENTORY_GAIN = "inventory_gain", "Inventory gain (A9)"
        BREAKAGE = "breakage", "Breakage (B18)"
        DUMP_TO_BULK = "dump_to_bulk", "Bottled dumped to bulk (A8 / B10)"
        EXPORT = "export", "Removed for export (B12)"
        TESTING = "testing", "Used for testing (A23 / B14)"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    phase = models.CharField(max_length=8, choices=Phase.choices, default=Phase.BOTTLED)
    tax_class = models.CharField(max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16)
    gallons = models.DecimalField(max_digits=10, decimal_places=1)
    occurred_at = models.DateField()
    lot = models.ForeignKey("cellar.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"{self.get_kind_display()} {self.gallons} gal {self.occurred_at}"
