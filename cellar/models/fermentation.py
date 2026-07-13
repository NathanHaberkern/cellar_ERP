"""
Fermentation module — destemming through fermentation ledger.

All append-only, all following the pattern Reading/Addition established.
Pressing, barrel-down, transfers, and the fortification event are the
next tranche (crush-out), not here.
"""
from django.conf import settings
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

    class Panel(models.TextChoices):
        # Classified at import: a sample with a Brix reading is a JUICE panel
        # (harvest), otherwise CHEMISTRY (spring racking). Heat-stability and
        # smoke sit apart; anything else lands in OTHER.
        JUICE = "juice", "Juice panel"
        CHEMISTRY = "chemistry", "Chemistry panel"
        HEAT_STABILITY = "heat_stability", "Heat stability"
        SMOKE = "smoke", "Smoke"
        OTHER = "other", "Other"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="lab_results")
    lab_request = models.ForeignKey(LabRequest, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")
    reported_at = models.DateTimeField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.ETS)
    panel = models.CharField(max_length=14, choices=Panel.choices, default=Panel.OTHER,
                             help_text="panel classification, set from the analytes present")
    # Outside-lab sample identifier — carried on ETS / Lodi results, blank in-house.
    sample_id = models.CharField(max_length=60, blank=True,
                                 help_text="outside-lab sample ID (ETS / Lodi); blank for in-house")

    @property
    def requires_sample_id(self):
        return self.source in (self.Source.ETS, self.Source.LODI)

    def __str__(self):
        return f"{self.lot} lab result {self.reported_at:%Y-%m-%d}"


class LabResultValue(AppendOnly):
    """One analyte reading on a result — the structured, calc-driving, exportable value.

    A raw ETS result is not always a bare number: censored readings ('< 0.05'),
    heat-stability pass/fail ('>20' NTU → FAIL), and 'Dry' glucose+fructose all
    have to survive import without collapsing to an ambiguous zero. So:
      * `value`   — the numeric reading the calcs use (0 for ND / Dry).
      * `qualifier` — the operator ETS printed ('<', '>', or '=' for a plain number).
      * `flag`    — the qualitative meaning (ND / Dry / Pass / FAIL / note).
      * `display` — exactly what the reports should show ('ND', 'Dry', 'FAIL',
                    or the number). This is the label; `value` is the math.
      * `raw_result` — the untouched source string, kept for audit.
    A plain numeric reading has qualifier '=', no flag, and display == the number.
    """
    class Qualifier(models.TextChoices):
        EQ = "=", "="
        LT = "<", "< (below detection / threshold)"
        GT = ">", "> (above threshold)"

    class Flag(models.TextChoices):
        NONE = "", "—"
        ND = "ND", "ND (not detected)"
        DRY = "Dry", "Dry"
        PASS = "Pass", "Pass (heat-stable)"
        FAIL = "FAIL", "FAIL (heat-unstable)"
        NOTE = "note", "Note"

    result = models.ForeignKey(LabResult, on_delete=models.CASCADE, related_name="values")
    analyte = models.ForeignKey("cellar.LabAnalyte", on_delete=models.PROTECT, related_name="+")
    value = models.DecimalField(max_digits=10, decimal_places=3,
                                help_text="numeric reading used by calcs; 0 for ND / Dry")
    qualifier = models.CharField(max_length=1, choices=Qualifier.choices, default=Qualifier.EQ)
    flag = models.CharField(max_length=8, choices=Flag.choices, blank=True, default=Flag.NONE)
    display = models.CharField(max_length=40, blank=True,
                               help_text="how the reading is shown (ND / Dry / FAIL / the number)")
    raw_result = models.CharField(max_length=200, blank=True,
                                  help_text="untouched source string, for audit")

    def __str__(self):
        return f"{self.analyte} = {self.display or self.value}"


class CellarNote(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="cellar_notes")
    noted_at = models.DateTimeField()
    body = models.TextField()

    def __str__(self):
        return f"{self.lot} note {self.noted_at:%Y-%m-%d}"


class LabSampleAlias(models.Model):
    """Binds an outside-lab 'Sample Description' string onto a Lot.

    The importer's normal path computes each lot's code and matches the description
    against it. That breaks whenever the string the cellar typed on the submission
    form isn't the lot's code — historical samples submitted under a provisional
    code ("25T" before the abbreviation catalog existed, now "25TROU"), shorthand,
    or a plain typo. Rather than force a re-designation to chase the lab's string,
    bind the string once here and every future import of it resolves silently.

    Created from the import preview when the user picks a lot for an unresolved
    sample. Not append-only: a mis-binding should be correctable in place.
    """
    description = models.CharField(max_length=120, unique=True,
                                   help_text="exact 'Sample Description' as the lab prints it")
    lot = models.ForeignKey("cellar.Lot", on_delete=models.CASCADE, related_name="sample_aliases")

    class Meta:
        verbose_name_plural = "lab sample aliases"

    def __str__(self):
        return f"{self.description} → {self.lot}"


class LotFermentationOverride(models.Model):
    """Per-lot override for fermentation-estimate parameters.

    Currently just the mandatory minimum skin-contact days used to floor the
    estimated press date on red skin-contact paths (see
    services.fermentation.skin_contact_floor_date). Deliberately a separate,
    always-editable row rather than a field on DestemmingEvent: the
    destemming record is append-only and documents what actually happened at
    crush, while this is a tunable *planning* parameter you may want to
    adjust mid-ferment (e.g. deciding on a longer extended maceration) without
    touching the historical record of how the lot was processed.

    Blank/null min_skin_contact_days falls back to the winery-wide
    ConfigConstant default — this row only needs to exist when a lot departs
    from that default.
    """
    lot = models.OneToOneField("cellar.Lot", on_delete=models.CASCADE,
                               related_name="fermentation_override")
    min_skin_contact_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="blank → use the winery-wide default")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"{self.lot} fermentation override"
