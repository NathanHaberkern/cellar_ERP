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

    class Room(models.TextChoices):
        OLD_TANK = "old_tank", "Old Tank Room"
        NEW_TANK = "new_tank", "New Tank Room"
        NEW_BARREL = "new_barrel", "New Barrel Room"

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
    # Dashboard tank-map placement (data-driven; seeded by `seed_vessel_layout`).
    # Bins carry no fixed placement — they surface in the barrel-room strip only while filled.
    room = models.CharField(max_length=12, choices=Room.choices, blank=True)
    map_row = models.PositiveSmallIntegerField(null=True, blank=True)
    map_col = models.PositiveSmallIntegerField(null=True, blank=True)
    # The ERP's record of what the glycol dial SHOULD read right now — set by
    # the glycol tasks (cold soak / settling / off / standard), not a live
    # sensor reading. Null = not currently being tracked (glycol off, or the
    # vessel isn't temp_controlled).
    glycol_setpoint_f = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
        help_text="Target glycol setpoint (°F) — informational; the human still sets the dial.")

    def __str__(self):
        return self.code


class Additive(models.Model):
    class Category(models.TextChoices):
        SO2 = "so2", "SO₂ / KMBS"
        ACID = "acid", "Acid"
        TANNIN = "tannin", "Tannin"
        ENZYME = "enzyme", "Enzyme"
        NUTRIENT = "nutrient", "Nutrient"
        YEAST = "yeast", "Yeast"
        FINING = "fining", "Fining agent"
        OTHER = "other", "Other"

    class DoseMode(models.TextChoices):
        PER_VOLUME = "per_volume", "Rate per volume"     # rate_unit: lb/1000gal, g/hL, L/1000gal, mL/hL
        PER_TON = "per_ton", "Rate per ton of fruit"     # rate_unit: mL/ton, g/ton
        PPM_TARGET = "ppm_target", "SO₂ to target ppm"   # KMBS dosed to a target added ppm
        # Dosed as a PERCENT of the lot's current volume; the computed quantity is
        # therefore GALLONS of liquid, and the addition grosses the lot up by that
        # much (see operations.adds_volume / record_addition). Water is the case
        # this exists for — "add 10% H2O" against 870 gal is 87 gal in, 957 out.
        PCT_VOLUME = "pct_volume", "Percent of volume (adds volume)"
        BENCH = "bench", "Bench trial (no default)"

    name = models.CharField(max_length=80, unique=True)
    category = models.CharField(max_length=12, choices=Category.choices)
    unit = models.CharField(max_length=20, help_text="costing/inventory unit, e.g. g, kg, lb, mL, L")
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True,
                                    help_text="seed/fallback cost per unit; the live figure is the "
                                              "stock ledger's weighted average once receipts exist")
    # Water (and bench trials) are dosed but not purchased as stock, so they must not
    # draw an ISSUE. Everything else defaults to tracked.
    track_stock = models.BooleanField(default=True,
                                      help_text="draw this from consumable inventory when added")
    # --- dosing metadata: drives the autopopulated default on an addition ---
    dose_mode = models.CharField(max_length=12, choices=DoseMode.choices,
                                 default=DoseMode.PER_VOLUME)
    default_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True,
                                       help_text="numeric default rate; its unit is rate_unit")
    rate_unit = models.CharField(max_length=20, blank=True,
                                 help_text="lb/1000gal · g/hL · L/1000gal · mL/hL · mL/ton")
    default_target_ppm = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True,
                                             help_text="ppm_target additives, e.g. SO₂ 40 at crush")
    so2_fraction = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True,
                                       help_text="ppm_target: SO₂ mass fraction of the product (KMBS 0.5764)")
    crush_addition = models.BooleanField(default=False,
        help_text="Show in the crush/intake additions picker (Section 5). "
                  "Everything else stays off that list but is still usable "
                  "from a lot's regular Additions tab.")

    def __str__(self):
        return self.name


class LabAnalyte(models.Model):
    # slug — stable machine key the importer and panel definitions reference, so a
    # display-name tweak never breaks a panel membership or a CSV mapping.
    slug = models.SlugField(max_length=40, unique=True, blank=True)
    name = models.CharField(max_length=40, unique=True)
    unit = models.CharField(max_length=20, blank=True)
    in_house = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(
        default=100, help_text="panel display order — lower shows first")

    class Meta:
        ordering = ("sort_order", "name")

    def __str__(self):
        return self.name


class LabAnalyteSynonym(models.Model):
    """Maps an outside lab's analysis-name string onto our canonical analyte.

    ETS reports the same reading under several names (ethanol at 20C / at 60F are
    kept separate; TA / VA / tartaric arrive with method suffixes). The importer
    looks a raw name up here first, then falls back to an exact analyte-name match.
    Editable in admin so a new ETS label never needs a code change.
    """
    raw_name = models.CharField(max_length=120, unique=True,
                                help_text="exact 'Analysis Name' string as ETS prints it")
    analyte = models.ForeignKey(LabAnalyte, on_delete=models.CASCADE, related_name="synonyms")

    def __str__(self):
        return f"{self.raw_name} → {self.analyte.slug}"


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


class FruitPrice(models.Model):
    """Contract price per ton, by vintage.

    Prices change every year, so they can't live on the Block as a single field —
    2025's Zinfandel at $1,600/ton must still be $1,600/ton when you look at the
    lot's COGS in 2029. Keyed on (vintage, variety) with an optional block, so a
    block-specific contract (Martel Cabernet at $2,000 against a general Cabernet
    price) wins over the varietal default.

    Estate fruit is priced the same way — one row per vintage, entered from that
    year's farming cost, until the farming module can compute it.
    """
    vintage_year = models.PositiveSmallIntegerField()
    variety = models.ForeignKey(Variety, on_delete=models.PROTECT, related_name="prices")
    block = models.ForeignKey(Block, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="prices",
                              help_text="blank = the varietal price for that vintage")
    price_per_ton = models.DecimalField(max_digits=9, decimal_places=2)
    notes = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [("vintage_year", "variety", "block")]
        ordering = ["-vintage_year", "variety__name"]

    @classmethod
    def for_lot(cls, vintage_year, variety, block=None):
        """Block-specific price if there is one, else the varietal price."""
        if block is not None:
            row = cls.objects.filter(vintage_year=vintage_year, variety=variety,
                                     block=block).first()
            if row:
                return row.price_per_ton
        row = cls.objects.filter(vintage_year=vintage_year, variety=variety,
                                 block__isnull=True).first()
        return row.price_per_ton if row else None

    def __str__(self):
        who = f"{self.variety} {self.block}" if self.block_id else str(self.variety)
        return f"{self.vintage_year} {who} — ${self.price_per_ton}/ton"
