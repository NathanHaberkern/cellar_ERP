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

    @property
    def cost(self):
        if self.quantity is None or self.additive.unit_cost is None:
            return 0
        return self.quantity * self.additive.unit_cost

    def __str__(self):
        return f"{self.lot} + {self.additive} ({self.target})"
