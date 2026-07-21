"""Master data: varieties, the abbreviation catalog, sources, vessels, additives, config."""
from decimal import Decimal

from django.db import models
from .base import AppendOnly, Program, SourceType


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

    WHY `basis` AND `source_ref`
    ---------------------------
    When the vineyard and the winery are separate entities under common control,
    the price on this row is a related-party transfer price and has to be defensible
    as arm's length. Three years later nobody remembers whether $1,600/ton came from
    a signed third-party contract, a comparable sale of the same block, or a district
    average off the Grape Crush Report — and "we don't remember" is the wrong answer
    to give an examiner. `basis` records the METHOD, `source_ref` records the
    DOCUMENT. Both travel with the price for the life of the vintage.

    WHY `is_provisional`
    --------------------
    The Grape Crush Report for a vintage doesn't publish until February (preliminary)
    and March (final) of the FOLLOWING year, so fruit delivered in September can only
    be priced against the prior year's district average. A price booked on that basis
    is provisional: it is the best number available on the day, not the final one.
    Flagging it here is what lets `fruit_price_trueup_report()` find the rows still
    owed a true-up once the report lands, instead of relying on somebody's memory.

    The price on THIS row is never rewritten by the true-up. It is what was booked,
    and it stays what was booked — see FruitPriceRevision.
    """

    class Basis(models.TextChoices):
        ARMS_LENGTH_SALE = "arms_length_sale", "Arm's-length sale (same vintage)"
        CONTRACT = "contract", "Third-party purchase contract"
        DISTRICT_AVERAGE = "district_average", "Grape Crush Report — same vintage"
        PRIOR_YEAR_DISTRICT = "prior_year_district", "Grape Crush Report — prior vintage"
        FARMING_COST = "farming_cost", "Actual farming cost"
        NEGOTIATED = "negotiated", "Negotiated / other"

    vintage_year = models.PositiveSmallIntegerField()
    variety = models.ForeignKey(Variety, on_delete=models.PROTECT, related_name="prices")
    block = models.ForeignKey(Block, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="prices",
                              help_text="blank = the varietal price for that vintage")
    price_per_ton = models.DecimalField(max_digits=9, decimal_places=2)
    basis = models.CharField(max_length=20, choices=Basis.choices, default=Basis.CONTRACT,
                             help_text="how this price was arrived at")
    source_ref = models.CharField(max_length=160, blank=True,
                                  help_text="the document behind it — contract or invoice no., "
                                            "or 'Grape Crush Report 2025 Final, District 11, Zinfandel'")
    is_provisional = models.BooleanField(default=False,
                                         help_text="priced on data that will be superseded; "
                                                   "owed a true-up when the final figure publishes")
    notes = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [("vintage_year", "variety", "block")]
        ordering = ["-vintage_year", "variety__name"]

    @classmethod
    def row_for_lot(cls, vintage_year, variety, block=None):
        """The FruitPrice ROW that governs — block-specific if there is one.

        `for_lot()` returns just the dollars and is what the costing chain has always
        called. The true-up needs the row itself (to reach its revisions), so the
        lookup lives here once and `for_lot()` delegates. Two implementations of the
        same block-beats-varietal precedence would drift, and the one that drifted
        would silently misprice fruit.
        """
        if block is not None:
            row = cls.objects.filter(vintage_year=vintage_year, variety=variety,
                                     block=block).first()
            if row:
                return row
        return cls.objects.filter(vintage_year=vintage_year, variety=variety,
                                  block__isnull=True).first()

    @classmethod
    def for_lot(cls, vintage_year, variety, block=None):
        """Block-specific price if there is one, else the varietal price."""
        row = cls.row_for_lot(vintage_year, variety, block)
        return row.price_per_ton if row else None

    def live_revision(self):
        """The one live final-price revision on this row, or None."""
        return self.revisions.filter(voided_at__isnull=True).order_by("-id").first()

    @property
    def final_price_per_ton(self):
        """What the fruit ended up costing: the revision if there is one, else as-booked."""
        rev = self.live_revision()
        return rev.final_price_per_ton if rev else self.price_per_ton

    @property
    def trueup_delta_per_ton(self):
        """Signed dollars/ton still to be booked. Zero when there's no revision."""
        rev = self.live_revision()
        if rev is None:
            return Decimal("0")
        return rev.final_price_per_ton - self.price_per_ton

    def __str__(self):
        who = f"{self.variety} {self.block}" if self.block_id else str(self.variety)
        return f"{self.vintage_year} {who} — ${self.price_per_ton}/ton"


class FruitPriceRevision(AppendOnly):
    """The final price for a vintage's fruit, booked after the provisional one.

    WHY THIS IS A SEPARATE ROW AND NOT AN EDIT
    ------------------------------------------
    The obvious implementation is to reach into the FruitPrice row in March and
    change `price_per_ton` to the published figure. That is wrong three times over:

      1. It restates history. `FruitPrice` is read live by `costing.fruit_cost()`,
         so overwriting it silently moves the fruit cost of every lot from that
         vintage — including lots whose cost has already been posted, reported, and
         summarised into a QBO journal entry for a CLOSED month.
      2. It breaks reconciliation. `cost_ledger.reconcile()` diffs posted cost
         against freshly computed cost. Repricing the source moves `computed` while
         `posted` stays put, so every lot in the vintage reports as drifted and the
         signal that reconciliation exists to give is buried in noise.
      3. It destroys the evidence. The whole point of pricing provisionally is that
         you booked the best number available on the day. If the row now says the
         final number, there is nothing left to show that you did.

    So the true-up is ADDITIVE: this row records the final price, the delta is
    derived, and `costing.fruit_trueup_cost()` books the difference as its own
    dated slice of fruit cost. The provisional price stays on the FruitPrice row
    as the record of what was booked at delivery, and both numbers are visible.

    The delta is signed. In a falling market the final figure comes in under the
    provisional one and the true-up is a CREDIT — which is the normal case for a
    prior-year-district-average basis after an oversupplied vintage.
    """

    price = models.ForeignKey(FruitPrice, on_delete=models.PROTECT,
                              related_name="revisions")
    final_price_per_ton = models.DecimalField(max_digits=9, decimal_places=2)
    basis = models.CharField(max_length=20, choices=FruitPrice.Basis.choices,
                             default=FruitPrice.Basis.DISTRICT_AVERAGE,
                             help_text="how the FINAL price was arrived at")
    source_ref = models.CharField(max_length=160, blank=True,
                                  help_text="the published figure this came from — e.g. "
                                            "'Grape Crush Report 2026 Final, District 11, Zinfandel'")
    effective_on = models.DateField(
        help_text="business date the true-up is booked — normally the date the final "
                  "report published, NOT the delivery date")

    class Meta:
        ordering = ("-effective_on", "-id")
        constraints = [
            # One live revision per price row. A second correction supersedes the
            # first by voiding it, so the delta is never ambiguous.
            models.UniqueConstraint(
                fields=["price"], condition=models.Q(voided_at__isnull=True),
                name="fruitpricerevision_one_live_per_price"),
        ]

    @property
    def delta_per_ton(self):
        return self.final_price_per_ton - self.price.price_per_ton

    def __str__(self):
        return (f"{self.price} → ${self.final_price_per_ton}/ton "
                f"({self.delta_per_ton:+}) {self.effective_on}")
