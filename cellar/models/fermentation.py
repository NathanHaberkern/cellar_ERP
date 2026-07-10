"""
Fermentation module — destemming through fermentation ledger.

All append-only, all following the pattern Reading/Addition established.
Pressing, barrel-down, transfers, and the fortification event are the
next tranche (crush-out), not here.
"""
from django.db import models
from .base import AppendOnly


class Severity(models.TextChoices):
    NONE = "none", "None"
    LIGHT = "light", "Light"
    MODERATE = "moderate", "Moderate"
    HEAVY = "heavy", "Heavy"


class DestemmingEvent(AppendOnly):
    class Path(models.TextChoices):
        A = "A", "A · White (destemmed)"
        B = "B", "B · Rosé (destemmed)"
        C = "C", "C · Rosé (direct press)"
        D = "D", "D · Red (destemmed)"
        E = "E", "E · Red (whole cluster)"
        F = "F", "F · White (whole cluster)"

    class Fruit(models.TextChoices):
        DESTEMMED = "destemmed", "Destemmed"
        WHOLE_BERRY = "whole_berry", "Whole berry"
        WHOLE_CLUSTER = "whole_cluster", "Whole cluster"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="destemmings")
    destem_at = models.DateTimeField()
    processing_path = models.CharField(max_length=1, choices=Path.choices,
                                       help_text="processing path per Destemming SOP")
    crusher_enabled = models.BooleanField(default=True)
    fruit_condition = models.CharField(max_length=14, choices=Fruit.choices, default=Fruit.DESTEMMED)
    foot_tread = models.BooleanField(default=False)
    foot_tread_pct = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
                                         help_text="% of the lot foot-tread, by bin count (3 of 6 bins → 50)")
    hold_hours = models.PositiveIntegerField(null=True, blank=True)
    initial_temp_f = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    mog_severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.NONE,
                                    help_text="material other than grapes")
    rot_type = models.CharField(max_length=40, blank=True)
    rot_severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.NONE)

    def __str__(self):
        return f"{self.lot} destemmed {self.destem_at:%Y-%m-%d}"


class TankAssignment(AppendOnly):
    """New record on each (re)assignment. A vessel holds one lot at a time;
    a lot may span vessels."""
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="tank_assignments")
    vessel = models.ForeignKey("cellar.Vessel", on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField()
    # Set when the lot leaves this vessel (moved / pressed / racked out). The tank map
    # reads a vessel's current lot as its assignment with emptied_at still null.
    emptied_at = models.DateTimeField(null=True, blank=True)
    CLOSE_FIELDS = ("emptied_at",)

    def __str__(self):
        return f"{self.lot} → {self.vessel}"


class ColdSoakSchedule(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="cold_soaks")
    start_at = models.DateTimeField()
    target_inoc_date = models.DateField(null=True, blank=True)
    skipped = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.lot} cold soak from {self.start_at:%Y-%m-%d}"


class PumpOverEvent(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="pump_overs")
    vessel = models.ForeignKey("cellar.Vessel", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    duration_min = models.PositiveIntegerField(null=True, blank=True,
                                               help_text="blank → vessel default")

    def __str__(self):
        return f"{self.lot} pump-over {self.started_at:%Y-%m-%d %H:%M}"


class PunchDownEvent(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="punch_downs")
    vessel = models.ForeignKey("cellar.Vessel", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    foot_tread = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.lot} punch-down {self.occurred_at:%Y-%m-%d %H:%M}"


class InoculationEvent(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="inoculations")
    vessel = models.ForeignKey("cellar.Vessel", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="+")
    inoculated_at = models.DateTimeField()
    native = models.BooleanField(default=False, help_text="native ⇒ no yeast/GoFerm")
    yeast_strain = models.CharField(max_length=60, blank=True)
    goferm = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.lot} inoculated ({'native' if self.native else self.yeast_strain})"


class LabRequest(AppendOnly):
    class Panel(models.TextChoices):
        ETS_JUICE = "ets_juice", "ETS juice panel"
        IN_HOUSE = "in_house", "In-house"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="lab_requests")
    sample_pulled_at = models.DateTimeField()
    panel_type = models.CharField(max_length=12, choices=Panel.choices)

    def __str__(self):
        return f"{self.lot} lab request {self.sample_pulled_at:%Y-%m-%d}"


class LabResult(AppendOnly):
    class Source(models.TextChoices):
        IN_HOUSE = "in_house", "In-house"
        ETS = "ets", "ETS"
        LODI = "lodi", "Lodi Wine Labs"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="lab_results")
    lab_request = models.ForeignKey(LabRequest, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")
    reported_at = models.DateTimeField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.ETS)
    # Outside-lab sample identifier — carried on ETS / Lodi results, blank in-house.
    sample_id = models.CharField(max_length=60, blank=True,
                                 help_text="outside-lab sample ID (ETS / Lodi); blank for in-house")

    @property
    def requires_sample_id(self):
        return self.source in (self.Source.ETS, self.Source.LODI)

    def __str__(self):
        return f"{self.lot} lab result {self.reported_at:%Y-%m-%d}"


class LabResultValue(AppendOnly):
    """One analyte reading on a result — the structured, calc-driving, exportable value."""
    result = models.ForeignKey(LabResult, on_delete=models.CASCADE, related_name="values")
    analyte = models.ForeignKey("cellar.LabAnalyte", on_delete=models.PROTECT, related_name="+")
    value = models.DecimalField(max_digits=10, decimal_places=3)

    def __str__(self):
        return f"{self.analyte} = {self.value}"


class CellarNote(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="cellar_notes")
    noted_at = models.DateTimeField()
    body = models.TextField()

    def __str__(self):
        return f"{self.lot} note {self.noted_at:%Y-%m-%d}"
