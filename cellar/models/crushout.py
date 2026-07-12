"""
Crush-out tranche — pressing → fortification → book-to-bond.

The compliance-bearing end of harvest. Encodes the decisions we settled:
  * volume of record = highest-confidence measurement (pressure > backfill > gpi)
  * fortification booked when T is determined (press gauge or barrel backfill)
  * tax class set from the fortification TARGET, not a lab ABV (measured later)
  * the base wine is BACKED OUT (T − spirit WG), so col (a) self-zeroes
  * the spirit draw is posted to the HPGS account in proof gallons
"""
from decimal import Decimal

from django.db import models, transaction
from .base import AppendOnly
from .spirits import HighProofSpiritLedger


class TaxClass(models.TextChoices):
    NOT_OVER_16 = "a", "Not over 16% (col a)"
    OVER_16_21 = "b", "Over 16–21% (col b)"
    OVER_21_24 = "c", "Over 21–24% (col c)"


class VolumeMeasurement(AppendOnly):
    """A measured volume for a lot. Barrel-backfill rows are the barrel-down;
    pressure-sensor rows are a direct tank gauge. The booking volume is the
    highest-confidence one (or the one explicitly flagged)."""
    class Method(models.TextChoices):
        PRESSURE_SENSOR = "pressure_sensor", "Pressure sensor"
        BARREL_BACKFILL = "barrel_backfill", "Barrel backfill"
        GPI_STRAP = "gpi_strap", "GPI strap"
        STATED = "stated", "Stated"

    _CONFIDENCE = {"pressure_sensor": "high", "barrel_backfill": "medium",
                   "stated": "medium", "gpi_strap": "low"}
    _RANK = {"high": 0, "medium": 1, "low": 2}
    DEFAULT_BARREL_CAPACITY = Decimal("60")
    DEFAULT_HEADSPACE = Decimal("3")

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="volume_measurements")
    method = models.CharField(max_length=16, choices=Method.choices)
    measured_at = models.DateTimeField()
    volume_gal = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                     help_text="derived for barrel_backfill / gpi_strap; entered otherwise")
    # barrel-backfill inputs
    barrels_filled = models.PositiveIntegerField(null=True, blank=True)
    barrel_capacity = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    headspace_allowance = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    # gpi input
    gpi_inches = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    gal_per_inch = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    is_booking_volume = models.BooleanField(default=False,
                                            help_text="authoritative figure for compliance")

    @property
    def confidence(self):
        return self._CONFIDENCE[self.method]

    def save(self, *args, **kwargs):
        if self.method == self.Method.BARREL_BACKFILL and self.volume_gal in (None, ""):
            cap = self.barrel_capacity if self.barrel_capacity is not None else self.DEFAULT_BARREL_CAPACITY
            head = self.headspace_allowance if self.headspace_allowance is not None else self.DEFAULT_HEADSPACE
            self.barrel_capacity, self.headspace_allowance = cap, head
            self.volume_gal = (self.barrels_filled or 0) * (cap - head)
        elif self.method == self.Method.GPI_STRAP and self.volume_gal in (None, "") \
                and self.gpi_inches and self.gal_per_inch:
            self.volume_gal = self.gpi_inches * self.gal_per_inch
        super().save(*args, **kwargs)

    @classmethod
    def booking_volume_for(cls, lot):
        qs = list(cls.objects.filter(lot=lot, voided_at__isnull=True))
        if not qs:
            return None
        flagged = [m for m in qs if m.is_booking_volume]
        pool = flagged or qs
        return min(pool, key=lambda m: cls._RANK[m.confidence])

    def __str__(self):
        return f"{self.lot} {self.volume_gal} gal ({self.method})"


class PressingEvent(AppendOnly):
    class Disposition(models.TextChoices):
        GROSS_LEES = "gross_lees", "Rack off gross lees"
        TO_BARREL = "to_barrel", "To barrel"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="pressings")
    pressed_at = models.DateTimeField()
    free_run_gal = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True)
    press_gal = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True)
    recombined = models.BooleanField(default=True)
    settling_period_days = models.PositiveIntegerField(null=True, blank=True)
    disposition = models.CharField(max_length=12, choices=Disposition.choices, blank=True)
    volume = models.ForeignKey(VolumeMeasurement, null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+",
                               help_text="the post-press gauge, if any")

    def __str__(self):
        return f"{self.lot} pressed {self.pressed_at:%Y-%m-%d}"


class FortificationEvent(AppendOnly):
    """Booked when T is determined. Creates the HPGS draw and holds the 5120.17 figures.
    Enter PG drawn and the fortification target; the rest derives.

    TWO KINDS, and they report differently
    --------------------------------------
    INITIAL — Port fortified on skins. The base wine has just fermented, is under
    16%, and has never been booked to bond. It is PRODUCED into col (a) line 2 and
    immediately USED out of col (a) line 19; the finished wine is produced into
    col (b) line 4. The base self-zeroes in col (a), which is why St. Amant's filed
    reports (which put the base in col (b)) still balanced.

    ADJUSTMENT — spring racking alcohol adjustment. Nothing fermented. The base is
    wine that is ALREADY in a tax class, usually col (b). Reporting it as
    "produced by fermentation" in col (a) would invent production that never
    happened. So: line 19 in the base's OWN class, line 4 in the finished class,
    and nothing on line 2 at all.

    The base volume is also an INPUT for an adjustment, not a derivation. June 2025:
    6,823 gal of port in, 112.4 PG (64.6 WG) of spirit added, 6,876.32 gal out. Base
    + spirit = 6,887.6, so 11.3 gal was lost on the rack — and deriving base as
    (finished − spirit) would have silently absorbed that loss into the production
    figure instead of reporting it. Give both gauges; the service books the gap.
    """
    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial (fortified on skins)"
        ADJUSTMENT = "adjustment", "Alcohol adjustment (already in bond)"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="fortifications")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INITIAL)
    base_tax_class = models.CharField(
        max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16,
        help_text="the class the base wine was in BEFORE this event. Initial → (a): "
                  "fresh base wine is under 16%. Adjustment → whatever it already was.")
    fortified_on_skins_date = models.DateField()
    booked_at = models.DateField(help_text="volume-determination date (press gauge or barrel-down)")
    spirit_proof = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                       help_text="blank → current blended HPGS proof")
    proof_gallons_drawn = models.DecimalField(max_digits=10, decimal_places=2)
    finished_wg = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                      help_text="T; blank → the lot's booking-volume measurement")
    expected_tax_class = models.CharField(max_length=1, choices=TaxClass.choices,
                                          default=TaxClass.OVER_16_21,
                                          help_text="from the fortification target, not a lab ABV")
    # derived / stored
    spirit_wg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    base_wg = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    spirit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                      help_text="$ of spirit drawn (WG × current blended $/WG)")
    hpgs_draw = models.ForeignKey(HighProofSpiritLedger, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="+")

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if self.spirit_proof in (None, ""):
            # the blended proof is a quotient and arrives with float dust on it;
            # proof is reported to 2dp, so pin it there before it propagates into
            # spirit_wg and then into base_wg.
            self.spirit_proof = Decimal(
                str(HighProofSpiritLedger.current_blended_proof())).quantize(Decimal("0.01"))
        if self.spirit_proof:
            self.spirit_wg = (self.proof_gallons_drawn * 100 / self.spirit_proof).quantize(Decimal("0.01"))
        if self.finished_wg in (None, ""):
            vm = VolumeMeasurement.booking_volume_for(self.lot)
            if vm:
                self.finished_wg = vm.volume_gal
        if (self.base_wg in (None, "")
                and self.finished_wg is not None and self.spirit_wg is not None):
            # Only a derivation of LAST resort. Any real loss between the two gauges
            # gets swallowed here, so the service supplies base_wg explicitly whenever
            # the wine was gauged going in.
            self.base_wg = (self.finished_wg - self.spirit_wg).quantize(Decimal("0.1"))
        if creating and self.hpgs_draw_id is None:
            on_hand = HighProofSpiritLedger.on_hand_wg()
            if self.spirit_wg and self.spirit_wg > on_hand:
                raise ValueError(
                    f"HPGS account holds only {on_hand} WG; can't draw {self.spirit_wg} WG "
                    f"for this fortification. Record a spirit receipt first.")
            cost_per_wg = Decimal(str(HighProofSpiritLedger.current_cost_per_wg()))
            self.spirit_cost = (self.spirit_wg * cost_per_wg).quantize(Decimal("0.01")) if self.spirit_wg else None
            with transaction.atomic():
                draw = HighProofSpiritLedger.objects.create(
                    event_type=HighProofSpiritLedger.EventType.DRAW,
                    event_date=self.booked_at,
                    wine_gallons=-(self.spirit_wg or 0),
                    proof=self.spirit_proof,
                    proof_gallons=-self.proof_gallons_drawn,
                    cost=-(self.spirit_cost or 0),
                )
                self.hpgs_draw = draw
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    @property
    def needs_part_x(self):
        """Straddle: on-skins and booking dates in different reporting periods."""
        a, b = self.fortified_on_skins_date, self.booked_at
        return (a.year, a.month) != (b.year, b.month)

    @property
    def implied_loss(self):
        """base + spirit − finished. Wine that went in and did not come out — the
        racking loss on an alcohol adjustment. None if we can't compute it."""
        if self.base_wg is None or self.spirit_wg is None or self.finished_wg is None:
            return None
        return (self.base_wg + self.spirit_wg - self.finished_wg).quantize(Decimal("0.1"))

    def form_5120_17_lines(self):
        fin = self.get_expected_tax_class_display()
        base = self.get_base_tax_class_display()
        lines = {}
        if self.kind == self.Kind.INITIAL:
            lines[f"Part I {base} line 2 — Produced by Fermentation"] = self.base_wg
        lines[f"Part I {base} line 19 — Used for Addition of Wine Spirits"] = self.base_wg
        lines[f"Part I {fin} line 4 — Produced by Addition of Wine Spirits"] = self.finished_wg
        lines["Part III — Wine spirits used (proof gallons)"] = self.proof_gallons_drawn
        loss = self.implied_loss
        if loss:
            lines[f"Part I {base} line 29 — Losses (other than inventory)"] = loss
        return lines

    def yield_check(self):
        """Derived base wine vs. rough crush-yield estimate (tons × 165)."""
        tons = sum((a.allocated_net_lbs for a in self.lot.allocations.all()), Decimal(0)) / 2000
        est = tons * Decimal("165")
        if not est or self.base_wg is None:
            return None
        return float((self.base_wg - est) / est * 100)  # % deviation

    def __str__(self):
        return f"{self.lot} fortified {self.fortified_on_skins_date}"


class BookToBond(AppendOnly):
    """Straight-fermentation production booking (non-fortified lots)."""
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="bond_bookings")
    booked_at = models.DateField()
    gallons_produced = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                           help_text="blank → the lot's booking-volume measurement")
    tax_class = models.CharField(max_length=1, choices=TaxClass.choices, default=TaxClass.NOT_OVER_16)

    def save(self, *args, **kwargs):
        if self.gallons_produced in (None, ""):
            vm = VolumeMeasurement.booking_volume_for(self.lot)
            if vm:
                self.gallons_produced = vm.volume_gal
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.lot} → bond {self.gallons_produced} gal ({self.get_tax_class_display()})"
