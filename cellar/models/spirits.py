"""High-Proof Spirit (HPGS) inventory account — append-only, carried in WG and PG."""
from django.db import models
from django.db.models import Sum
from .base import AppendOnly


class HighProofSpiritLedger(AppendOnly):
    class EventType(models.TextChoices):
        RECEIPT = "receipt", "Receipt"
        DRAW = "draw", "Draw (fortification)"
        ADJUSTMENT = "adjustment", "Adjustment"
        LOSS = "loss", "Loss"

    event_type = models.CharField(max_length=12, choices=EventType.choices)
    event_date = models.DateField()
    wine_gallons = models.DecimalField(max_digits=10, decimal_places=2,
                                       help_text="signed: + receipt, − draw")
    proof = models.DecimalField(max_digits=6, decimal_places=2,
                                help_text="receipt: lot proof; draw: current blended proof")
    proof_gallons = models.DecimalField(max_digits=10, decimal_places=2,
                                        help_text="= wine_gallons × proof ÷ 100")
    supplier = models.CharField(max_length=120, blank=True)
    shipment_ref = models.CharField(max_length=60, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                               help_text="receipt: total $ paid; draw: −allocated $ (auto)")
    # fortification_event FK added with the fortification tranche

    def save(self, *args, **kwargs):
        if self.proof_gallons in (None, ""):
            self.proof_gallons = (self.wine_gallons or 0) * (self.proof or 0) / 100
        super().save(*args, **kwargs)

    @classmethod
    def on_hand_wg(cls):
        return cls.objects.filter(voided_at__isnull=True).aggregate(v=Sum("wine_gallons"))["v"] or 0

    @classmethod
    def on_hand_pg(cls):
        return cls.objects.filter(voided_at__isnull=True).aggregate(v=Sum("proof_gallons"))["v"] or 0

    @classmethod
    def current_blended_proof(cls):
        wg = cls.on_hand_wg()
        return (cls.on_hand_pg() / wg * 100) if wg else 0

    @classmethod
    def on_hand_cost(cls):
        return cls.objects.filter(voided_at__isnull=True).aggregate(v=Sum("cost"))["v"] or 0

    @classmethod
    def current_cost_per_wg(cls):
        wg = cls.on_hand_wg()
        return (cls.on_hand_cost() / wg) if wg else 0

    def __str__(self):
        return f"{self.event_type} {self.wine_gallons} WG @ {self.proof} ({self.event_date})"
