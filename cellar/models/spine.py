"""Core spine: HarvestEvent → WeighTag → WeighTagAllocation → Lot → LotDesignation, plus LotLineage."""
from django.conf import settings
from django.db import models
from .base import AppendOnly, LotKind, SourceType


class Severity(models.TextChoices):
    """4-level fruit-condition scale, shared by weigh-tag MOG/Rot ratings.
    Same string values as the destem-side scale so the two stay consistent."""
    NONE = "none", "None"
    LIGHT = "light", "Light"
    MODERATE = "moderate", "Moderate"
    HEAVY = "heavy", "Heavy"


class HarvestEvent(models.Model):
    block = models.ForeignKey("cellar.Block", on_delete=models.PROTECT, related_name="harvests")
    harvest_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"{self.block} · {self.harvest_date}"


class WeighTag(models.Model):
    """Header for a delivery. Weighed by bin/stack via WeighTagBin lines (estate);
    net-only tags (purchased) may carry net_weight_lbs directly with no bin lines.
    Immutable after crush."""
    class Disposition(models.TextChoices):
        CRUSHED = "crushed", "Crushed"
        SOLD = "sold", "Sold"

    weigh_tag_number = models.CharField(max_length=40, unique=True)
    harvest_event = models.ForeignKey(HarvestEvent, on_delete=models.PROTECT, related_name="weigh_tags")
    source_type = models.CharField(max_length=12, choices=SourceType.choices)
    disposition = models.CharField(max_length=8, choices=Disposition.choices)
    gross_weight_lbs = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                           help_text="net-only tags: leave blank; use bin lines instead")
    tare_weight_lbs = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)
    net_weight_lbs = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                         help_text="only for net-only (purchased) tags with no bin lines")
    third_party_scale = models.BooleanField(default=False)
    brix_at_receipt = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    purchase_price_per_ton = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True,
        help_text="purchased fruit only — for the CA Grape Crush Report")
    fruit_cost_per_ton = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True,
        help_text="accounting cost basis (estate farming cost, or purchase price) — for COGS")
    locked = models.BooleanField(default=False, help_text="set true at crush")
    # Per-delivery fruit assessment (rated once at the tag, not per resulting lot).
    mog_severity = models.CharField(max_length=8, choices=Severity.choices,
                                    default=Severity.NONE, help_text="material other than grapes")
    rot_severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.NONE)
    rot_type = models.CharField(max_length=40, blank=True, help_text="e.g. botrytis, sour")
    notes = models.TextField(blank=True, help_text="free-form intake notes for this delivery")
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    def _bin_lines(self):
        return list(self.bins.all())

    @property
    def gross_total(self):
        lines = self._bin_lines()
        return sum((b.gross_lbs for b in lines), 0) if lines else (self.gross_weight_lbs or 0)

    @property
    def net_total(self):
        lines = self._bin_lines()
        return sum((b.net_lbs or 0 for b in lines), 0) if lines else (self.net_weight_lbs or 0)

    @property
    def net_tons(self):
        return float(self.net_total) / 2000.0

    @property
    def allocated_lbs(self):
        return sum((a.allocated_net_lbs for a in self.allocations.filter(voided_at__isnull=True)), 0)

    @property
    def remaining_lbs(self):
        return self.net_total - self.allocated_lbs

    def __str__(self):
        return self.weigh_tag_number


class WeighTagBin(models.Model):
    """One weighing line — a single bin or a 2-bin stack — with its own gross and net.
    Feeds the labor contractor's per-worker payment calc. Tare = bin_count × 98 lb."""
    TARE_PER_BIN = 98

    weigh_tag = models.ForeignKey(WeighTag, on_delete=models.CASCADE, related_name="bins")
    assigned_lot = models.ForeignKey("cellar.Lot", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="assigned_bins",
                                     help_text="the lot this bin was crushed into, if assigned")
    bin_label = models.CharField(max_length=40, help_text="bin number(s) in this weighing, e.g. '22/142'")
    bin_count = models.PositiveSmallIntegerField(default=2, help_text="bins in this weighing (1 or 2)")
    gross_lbs = models.DecimalField(max_digits=10, decimal_places=1)
    net_lbs = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                  help_text="blank → gross − bin_count × 98")

    def save(self, *args, **kwargs):
        if self.net_lbs in (None, ""):
            self.net_lbs = self.gross_lbs - self.bin_count * self.TARE_PER_BIN
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.bin_label}: {self.gross_lbs} gross / {self.net_lbs} net"


class Lot(models.Model):
    """Immutable surrogate key; the human code lives on the current LotDesignation.
    Lot itself is mutable (status advances over the lot's life)."""
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        RECEIVING = "receiving", "Receiving"
        PROCESSING = "processing", "Processing"
        COLD_SOAK = "cold_soak", "Cold soak"
        FERMENTING = "fermenting", "Fermenting"
        PRESSED = "pressed", "Pressed"
        SETTLING = "settling", "Settling"
        DONE_PRIMARY = "done_primary", "Primary complete"
        BOTTLED = "bottled", "Bottled"

    vintage_year = models.PositiveSmallIntegerField(help_text="2-digit, = harvest year")
    current_designation = models.ForeignKey(
        "cellar.LotDesignation", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.RECEIVING)
    production_intent = models.TextField(blank=True, help_text="free text at crush")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    @property
    def code(self):
        from cellar.services.generator import render_designation
        return render_designation(self.current_designation) if self.current_designation else "(unassigned)"

    def __str__(self):
        return self.code


class LotDesignation(models.Model):
    """Temporal record of a lot's code. Re-designation = new row; the prior row's
    effective_to is closed. Components are the source of truth; the string is rendered."""
    class Reason(models.TextChoices):
        INITIAL = "initial", "Initial"
        REDESIGNATION = "redesignation_program_change", "Re-designation (program change)"

    lot = models.ForeignKey(Lot, on_delete=models.CASCADE, related_name="designations")
    kind = models.CharField(max_length=12, choices=LotKind.choices)
    members = models.JSONField(default=list,
                               help_text="[{abbr, seq}] — seq null for co-ferment components")
    custom_suffix = models.CharField(max_length=40, blank=True)
    is_provisional = models.BooleanField(default=False)
    reason = models.CharField(max_length=32, choices=Reason.choices, default=Reason.INITIAL)
    effective_from = models.DateTimeField(auto_now_add=True)
    effective_to = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        from cellar.services.generator import render_designation
        return render_designation(self)


class LotSectionNote(models.Model):
    """Mutable free-form scratchpad for a lot, one per page section (overview +
    the six detail sub-pages). Unlike the append-only ledger rows, this is a
    living note the cellar edits in place — so it's a plain model, upserted by
    (lot, section)."""
    class Section(models.TextChoices):
        OVERVIEW = "overview", "Overview"
        ADDITIONS = "additions", "Additions"
        LABS = "labs", "Labs"
        MOVEMENT = "movement", "Movement"
        COMPOSITION = "composition", "Composition"
        OAK = "oak", "Oak"

    lot = models.ForeignKey(Lot, on_delete=models.CASCADE, related_name="section_notes")
    section = models.CharField(max_length=16, choices=Section.choices)
    body = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    class Meta:
        unique_together = [("lot", "section")]

    def __str__(self):
        return f"{self.lot} · {self.get_section_display()} note"


class LotCompositionOverride(models.Model):
    """Manual composition percentages for label/marketing use, stated independently
    of the computed genealogy on the Composition tab.

    `composition_of()` in aging.py derives exact percentages from LotLineage — the
    real, ledger-backed record of what was blended into what. That figure is
    correct for compliance but isn't always what a label wants to say: TTB
    varietal-labeling rules allow rounding and minimums that don't match the
    computed genealogy exactly, and some blends are described by house style
    rather than measured percentage. This is a SEPARATE, clearly-labeled stated
    value — it never feeds reporting and never overwrites the computed figure.

    One row per lot; `components` is [{label, pct}], entered free-form since the
    label copy may name a variety or region rather than a leaf lot code.
    """
    lot = models.OneToOneField(Lot, on_delete=models.CASCADE, related_name="composition_override")
    components = models.JSONField(default=list, help_text="[{label, pct}] — for label/marketing use")
    notes = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    def total_pct(self):
        return sum((float(c.get("pct") or 0) for c in (self.components or [])), 0.0)

    def __str__(self):
        return f"{self.lot} · label composition override"


class WeighTagAllocation(AppendOnly):
    """Many-to-many between weigh tags and lots, carrying allocated pounds."""
    weigh_tag = models.ForeignKey(WeighTag, on_delete=models.PROTECT, related_name="allocations")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="allocations")
    allocated_net_lbs = models.DecimalField(max_digits=10, decimal_places=1)

    def __str__(self):
        return f"{self.weigh_tag} → {self.lot} ({self.allocated_net_lbs} lb)"


class LotLineage(AppendOnly):
    """Genealogy edges for splits and blends (co-ferment multi-variety is captured via allocations)."""
    class Relationship(models.TextChoices):
        SPLIT_SAIGNEE = "split_saignee", "Split — saignée"
        SPLIT_DRAINOFF = "split_drainoff", "Split — drain-off"
        WHOLE_BLEND = "whole_blend", "Whole-lot blend"
        PARTIAL_BLEND = "partial_blend_contribution", "Partial blend contribution"
        TOPPING = "topping_contribution", "Topping contribution"
        BOTTLING_SPLIT = "bottling_split", "Bottling parcel split"

    parent_lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="lineage_as_parent")
    child_lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="lineage_as_child")
    relationship_type = models.CharField(max_length=32, choices=Relationship.choices)
    volume_gal = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True)

    def __str__(self):
        return f"{self.parent_lot} → {self.child_lot} [{self.relationship_type}]"
