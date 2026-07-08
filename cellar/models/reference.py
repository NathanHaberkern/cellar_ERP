"""Master data: varieties, the abbreviation catalog, sources, vessels, additives, config."""
from django.db import models
from .base import Program, SourceType


class Variety(models.Model):
    name = models.CharField(max_length=80, unique=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "varieties"

    def __str__(self):
        return self.name


class Grower(models.Model):
    name = models.CharField(max_length=120, unique=True)
    source_type = models.CharField(max_length=12, choices=SourceType.choices)

    def __str__(self):
        return self.name


class Vineyard(models.Model):
    grower = models.ForeignKey(Grower, on_delete=models.PROTECT, related_name="vineyards")
    name = models.CharField(max_length=120)
    crush_district = models.PositiveSmallIntegerField(null=True, blank=True,
        help_text="CA Grape Crush Report district (e.g. 10 Amador, 11 Lodi)")
    crush_report_district = models.CharField(
        max_length=60, blank=True, help_text="CA Grape Crush Report pricing district")

    class Meta:
        unique_together = [("grower", "name")]

    def __str__(self):
        return self.name


class Block(models.Model):
    vineyard = models.ForeignKey(Vineyard, on_delete=models.PROTECT, related_name="blocks")
    variety = models.ForeignKey(Variety, on_delete=models.PROTECT, related_name="blocks",
                                help_text="A block is a single variety.")
    name = models.CharField(max_length=60, help_text="e.g. '23 Rows', '422'")
    acreage = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = [("vineyard", "name")]

    def __str__(self):
        return f"{self.vineyard} · {self.name} ({self.variety})"


class VarietalDesignation(models.Model):
    """Curated abbreviation catalog. Resolution precedence: block > vineyard > variety default."""
    variety = models.ForeignKey(Variety, on_delete=models.PROTECT, related_name="designations")
    program = models.CharField(max_length=8, choices=Program.choices)
    abbreviation = models.CharField(max_length=20)
    block = models.ForeignKey(Block, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+", help_text="block-level override (e.g. 422 → MZ)")
    vineyard = models.ForeignKey(Vineyard, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+", help_text="vineyard-level override (e.g. Spencer → SRCS)")
    is_curated = models.BooleanField(default=True,
                                     help_text="False = provisional, auto-suggested, needs review")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["variety", "program", "block", "vineyard"],
                name="uniq_designation_key"),
        ]

    def __str__(self):
        return self.abbreviation


class Vessel(models.Model):
    class Type(models.TextChoices):
        TANK = "tank", "Tank"
        MACRO_BIN = "macro_bin", "Macro bin"
        ONE_TON_BIN = "one_ton_bin", "1-ton bin"

    class VolumeMethod(models.TextChoices):
        PRESSURE_SENSOR = "pressure_sensor", "Pressure sensor"
        GPI_STRAP = "gpi_strap", "GPI strap (low confidence)"
        NONE = "none", "Not gaugeable"

    code = models.CharField(max_length=30, unique=True)
    type = models.CharField(max_length=12, choices=Type.choices)
    capacity_gal = models.DecimalField(max_digits=8, decimal_places=1)
    max_fruit_tons = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    default_pumpover_min = models.PositiveIntegerField(null=True, blank=True)
    refrigerated = models.BooleanField(default=False)
    temp_controlled = models.BooleanField(default=False)
    tare_lbs = models.DecimalField(max_digits=8, decimal_places=1, null=True, blank=True)
    volume_method = models.CharField(max_length=16, choices=VolumeMethod.choices,
                                     default=VolumeMethod.NONE)
    gal_per_inch = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    sensor_calibration = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return self.code


class Additive(models.Model):
    class Category(models.TextChoices):
        SO2 = "so2", "SO₂ / KMBS"
        TANNIN = "tannin", "Tannin"
        ENZYME = "enzyme", "Enzyme"
        NUTRIENT = "nutrient", "Nutrient"
        YEAST = "yeast", "Yeast"
        OTHER = "other", "Other"

    name = models.CharField(max_length=80, unique=True)
    category = models.CharField(max_length=12, choices=Category.choices)
    unit = models.CharField(max_length=20)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True,
                                    help_text="cost per unit, for COGS")

    def __str__(self):
        return self.name


class LabAnalyte(models.Model):
    name = models.CharField(max_length=40, unique=True)
    unit = models.CharField(max_length=20, blank=True)
    in_house = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class ConfigConstant(models.Model):
    key = models.CharField(max_length=60, unique=True)
    value = models.CharField(max_length=60)
    unit = models.CharField(max_length=20, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.key} = {self.value}{self.unit}"


class LotSequenceCounter(models.Model):
    """Monotonic per-(vintage, abbreviation) counter. Only ever increments →
    numbers are never reused (voided lots leave a permanent gap), and a
    row-level lock (select_for_update) makes concurrent lot creation safe."""
    vintage = models.PositiveSmallIntegerField()
    abbreviation = models.CharField(max_length=20)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("vintage", "abbreviation")]

    def __str__(self):
        return f"{self.vintage}{self.abbreviation} @ {self.last_seq}"
