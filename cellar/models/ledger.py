"""
Fermentation ledger — pattern exemplars.

Reading and Addition show the shape every transaction type follows
(append-only base + lot/vessel + type-specific payload + occurred_at).
The remaining ~15 types — DestemmingEvent, TankAssignment, ColdSoakSchedule,
PumpOverEvent, PunchDownEvent, InoculationEvent, LabRequest, LabResult,
CellarNote, PressingEvent, BarrelDown, BlendTransfer, TankTransfer,
VolumeLoss, FortificationEvent, BookToBond — are the next tranche and
follow this identical pattern.
"""
from django.db import models
from .base import AppendOnly


class Reading(AppendOnly):
    class Analyte(models.TextChoices):
        BRIX = "brix", "Brix"
        TEMP = "temp", "Temperature (°F)"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="readings")
    vessel = models.ForeignKey("cellar.Vessel", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+",
                               help_text="set when a lot spans multiple vessels")
    analyte = models.CharField(max_length=8, choices=Analyte.choices)
    value = models.DecimalField(max_digits=6, decimal_places=2)
    method = models.CharField(max_length=40, blank=True)
    measured_at = models.DateTimeField()

    def __str__(self):
        return f"{self.lot} {self.analyte}={self.value} @ {self.measured_at:%Y-%m-%d}"


class Addition(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="additions")
    vessel = models.ForeignKey("cellar.Vessel", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+")
    additive = models.ForeignKey("cellar.Additive", on_delete=models.PROTECT, related_name="+")
    target = models.CharField(max_length=60, help_text="e.g. '40 ppm SO₂'")
    computed_dose = models.CharField(max_length=60, blank=True, help_text="e.g. '520 g KMBS'")
    quantity = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True,
                                   help_text="numeric amount used, for COGS")
    basis_snapshot = models.JSONField(default=dict, blank=True,
                                      help_text="inputs used: YAN, brix, yeast, volume")
    added_at = models.DateTimeField()
    # Weighted average at the instant of the addition, frozen. Before this field,
    # `cost` read additive.unit_cost LIVE, so repricing a bag of tartaric restated
    # every lot that ever used it. Same defect, same fix, as LotLineage (0027).
    unit_cost_snapshot = models.DecimalField(max_digits=12, decimal_places=6,
                                             null=True, blank=True)

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if creating and self.unit_cost_snapshot is None:
            from cellar.services import stock as stock_svc
            self.unit_cost_snapshot = stock_svc.wac(self.additive)
        super().save(*args, **kwargs)
        # Draw stock AFTER the row exists so the ISSUE can point back at it.
        # Idempotency matters here (cf. FortificationEvent's HPGS draw): guarded on
        # `creating` and on there being no existing ISSUE for this addition.
        if creating and self.quantity and getattr(self.additive, "track_stock", True):
            from cellar.services import stock as stock_svc
            if not self.stock_txns.filter(voided_at__isnull=True).exists():
                stock_svc.issue(self.additive, self.quantity,
                                occurred_at=self.added_at.date() if self.added_at else None,
                                addition=self, operator=self.operator)

    @property
    def cost(self):
        if self.quantity is None:
            return 0
        rate = self.unit_cost_snapshot
        if rate is None:
            rate = self.additive.unit_cost
        if rate is None:
            return 0
        return self.quantity * rate

    def __str__(self):
        return f"{self.lot} + {self.additive} ({self.target})"
