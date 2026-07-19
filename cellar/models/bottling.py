"""
Bottling tranche — bulk-to-bottle, dry goods, finished goods, tax-paid removals.

Bottling moves wine from in-bond bulk to bottled inventory (still in bond, now glass);
a tax-paid removal is the taxable event when it leaves for a customer/distributor.
COGS per bottle rolls up the wine cost (all upstream streams) + dry goods + line cost.

ROUNDING CONVENTION (TTB): gallon figures are kept at FULL precision through every
computation. The ml→gallon conversion (ML_PER_GALLON) is never rounded, and neither
are gal_per_bottle / case_gallons. Rounding happens once, at the report boundary, to
the precision the form requires — per 27 CFR 24.281, taxpaid removals are summarized
by tax class in wine gallons to the nearest TENTH gallon, and it's the summary that is
rounded, not each row (sum at full precision, then round). The per-record quantize()
calls below are display conveniences only; the reporting module must recompute from
full precision and round the summary line.
"""
from decimal import Decimal

from django.db import models
from .base import AppendOnly

ML_PER_GALLON = Decimal("3785.411784")   # exact legal definition — never round this


class BottleFormat(models.Model):
    name = models.CharField(max_length=40, unique=True)      # e.g. "750ml"
    ml = models.PositiveIntegerField()
    bottles_per_case = models.PositiveIntegerField()

    @property
    def gal_per_bottle(self):
        return Decimal(self.ml) / ML_PER_GALLON

    @property
    def case_gallons(self):
        return self.gal_per_bottle * self.bottles_per_case

    def __str__(self):
        return f"{self.name} ({self.bottles_per_case}/case)"


class DryGood(models.Model):
    class Kind(models.TextChoices):
        BOTTLE = "bottle", "Bottle"
        CLOSURE = "closure", "Closure (cork/screwcap)"
        CAPSULE = "capsule", "Capsule"
        LABEL = "label", "Label"
        OTHER = "other", "Other"

    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4)
    unit = models.CharField(max_length=20, default="each")

    def __str__(self):
        return self.name


class BottlingRun(AppendOnly):
    """One bottling of one bulk lot into one format. Produces finished goods (in bond)."""
    source_lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="bottlings")
    bottle_format = models.ForeignKey(BottleFormat, on_delete=models.PROTECT, related_name="+")
    sku = models.CharField(max_length=60, help_text="finished-goods SKU (matches C7 / QBO)")
    bottled_at = models.DateField()
    bulk_gallons_in = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                          help_text="blank → the lot's booking volume")
    cases_produced = models.PositiveIntegerField()
    line_labor_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                          help_text="bottling line + labor for this run")

    def save(self, *args, **kwargs):
        if self.bulk_gallons_in in (None, "") and self._state.adding:
            from cellar.models import VolumeMeasurement
            vm = VolumeMeasurement.booking_volume_for(self.source_lot)
            if vm:
                self.bulk_gallons_in = vm.volume_gal
        super().save(*args, **kwargs)

    @property
    def bottles_produced(self):
        return self.cases_produced * self.bottle_format.bottles_per_case

    @property
    def volume_bottled_gal(self):
        return (self.bottles_produced * self.bottle_format.gal_per_bottle).quantize(Decimal("0.1"))

    @property
    def bottling_loss_gal(self):
        if self.bulk_gallons_in is None:
            return None
        return (self.bulk_gallons_in - self.volume_bottled_gal).quantize(Decimal("0.1"))

    @property
    def cases_removed(self):
        return sum((r.cases for r in self.removals.filter(voided_at__isnull=True)), 0)

    @property
    def cases_on_hand(self):
        return self.cases_produced - self.cases_removed

    def __str__(self):
        return f"{self.sku} {self.bottled_at} ({self.cases_produced} cs)"


class BottlingDryGoodUse(AppendOnly):
    run = models.ForeignKey(BottlingRun, on_delete=models.PROTECT, related_name="dry_goods")
    dry_good = models.ForeignKey(DryGood, on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(max_digits=12, decimal_places=6,
                                             null=True, blank=True,
                                             help_text="WAC at the bottling run, frozen")

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if creating and self.unit_cost_snapshot is None:
            from cellar.services import stock as stock_svc
            self.unit_cost_snapshot = stock_svc.wac(self.dry_good)
        super().save(*args, **kwargs)
        if creating and self.quantity:
            from cellar.services import stock as stock_svc
            if not self.stock_txns.filter(voided_at__isnull=True).exists():
                stock_svc.issue(self.dry_good, self.quantity,
                                occurred_at=self.run.bottled_at,
                                dry_good_use=self, operator=self.operator)

    @property
    def cost(self):
        rate = self.unit_cost_snapshot
        if rate is None:
            rate = self.dry_good.unit_cost
        return self.quantity * (rate or 0)

    def __str__(self):
        return f"{self.run.sku}: {self.quantity} × {self.dry_good}"


class TaxPaidRemoval(AppendOnly):
    """The taxable event — bottled wine leaves bond. Drives the excise return."""
    class Channel(models.TextChoices):
        DTC = "dtc", "Direct-to-consumer"
        WHOLESALE = "wholesale", "Wholesale"
        OTHER = "other", "Other"

    bottling_run = models.ForeignKey(BottlingRun, on_delete=models.PROTECT, related_name="removals")
    removed_at = models.DateField()
    cases = models.PositiveIntegerField()
    channel = models.CharField(max_length=10, choices=Channel.choices)

    @property
    def bottles(self):
        return self.cases * self.bottling_run.bottle_format.bottles_per_case

    @property
    def wine_gallons_removed(self):
        return (self.bottles * self.bottling_run.bottle_format.gal_per_bottle).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.bottling_run.sku}: {self.cases} cs {self.get_channel_display()} {self.removed_at}"
