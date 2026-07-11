from datetime import date

from django import forms
from django.core.exceptions import ValidationError
from django.contrib import admin

from cellar.models import (
    Variety, Grower, Vineyard, Block, VarietalDesignation, Vessel,
    Additive, LabAnalyte, LabAnalyteSynonym, ConfigConstant, HarvestEvent, WeighTag, WeighTagBin,
    Lot, LotDesignation, WeighTagAllocation, HighProofSpiritLedger, Program,
    Reading, Addition, DestemmingEvent, TankAssignment, ColdSoakSchedule,
    PumpOverEvent, PunchDownEvent, InoculationEvent, LabRequest, LabResult,
    LabResultValue, CellarNote,
    VolumeMeasurement, PressingEvent, FortificationEvent, BookToBond,
)
from cellar.models import Task, TaskEvent, TaskRule
from cellar.services.generator import assign_initial_designation, render_designation
from django.contrib import messages
from django.utils import timezone


class AuditMixin(object):
    """Stamp created_by / operator from the logged-in user on new top-level records."""
    def save_model(self, request, obj, form, change):
        for f in ("created_by", "operator"):
            if hasattr(obj, f) and getattr(obj, f"{f}_id", None) is None:
                setattr(obj, f, request.user)
        super().save_model(request, obj, form, change)


@admin.action(description="Void selected (append-only correction)")
def void_entries(modeladmin, request, queryset):
    if not hasattr(queryset.model, "voided_at"):
        modeladmin.message_user(request, "No void support on this model.", level=messages.WARNING)
        return
    n = queryset.update(voided_at=timezone.now())
    modeladmin.message_user(request, f"Voided {n} entr{'y' if n == 1 else 'ies'}.")


def _alloc_status(net, allocated):
    if allocated <= 0:
        return "unallocated"
    if allocated < net:
        return "partial"
    return "full"


class AllocationStatusFilter(admin.SimpleListFilter):
    title = "allocation status"
    parameter_name = "alloc"

    def lookups(self, request, model_admin):
        return [("unallocated", "Unallocated"), ("partial", "Partial"),
                ("full", "Fully allocated")]

    def queryset(self, request, qs):
        want = self.value()
        if not want:
            return qs
        keep = [wt.pk for wt in qs if _alloc_status(wt.net_total, wt.allocated_lbs) == want]
        return qs.filter(pk__in=keep)


# ----------------------------------------------------------------- inlines
class AllocationForm(forms.ModelForm):
    """Blank lbs → allocate 100% of the tag's remaining weight; never over-allocate."""
    class Meta:
        model = WeighTagAllocation
        fields = ("weigh_tag", "allocated_net_lbs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allocated_net_lbs"].required = False
        self.fields["allocated_net_lbs"].help_text = "leave blank to allocate 100% of the tag"

    def clean(self):
        cleaned = super().clean()
        tag = cleaned.get("weigh_tag")
        amt = cleaned.get("allocated_net_lbs")
        if tag:
            remaining = tag.remaining_lbs   # tag.net_total − already allocated
            if amt in (None, ""):
                cleaned["allocated_net_lbs"] = remaining
            elif amt > remaining:
                raise ValidationError(
                    f"{tag} has only {remaining} lb unallocated "
                    f"(net {tag.net_total}); can't allocate {amt} lb.")
        return cleaned


class AllocationInline(admin.TabularInline):
    model = WeighTagAllocation
    form = AllocationForm
    fields = ("weigh_tag", "allocated_net_lbs")
    extra = 1
    can_delete = False   # allocations are append-only


class BinInline(admin.TabularInline):
    model = WeighTagBin
    fields = ("bin_label", "bin_count", "gross_lbs", "net_lbs")
    extra = 2


class DesignationInline(admin.TabularInline):
    model = LotDesignation
    fields = ("rendered", "kind", "reason", "is_provisional", "effective_from", "effective_to")
    readonly_fields = ("rendered", "kind", "reason", "is_provisional",
                       "effective_from", "effective_to")
    extra = 0
    can_delete = False

    @admin.display(description="code")
    def rendered(self, obj):
        return render_designation(obj) if obj and obj.pk else ""

    def has_add_permission(self, request, obj=None):
        return False


# ------------------------------------------------------------- lot add form
class LotAddForm(forms.ModelForm):
    """Adds the generator inputs to the Lot add screen. On save the admin creates
    the Lot row, then assign_initial_designation() mints the code."""
    gen_variety = forms.ModelChoiceField(
        queryset=Variety.objects.all(), label="Variety (for code)")
    gen_program = forms.ChoiceField(choices=Program.choices, label="Program")
    gen_block = forms.ModelChoiceField(
        queryset=Block.objects.all(), required=False,
        label="Block override (only Zin 422/416 etc.)")
    gen_vineyard = forms.ModelChoiceField(
        queryset=Vineyard.objects.all(), required=False,
        label="Vineyard override (only Cab Spencer/Martel)")

    class Meta:
        model = Lot
        fields = ["vintage_year", "status", "production_intent"]


@admin.register(Lot)
class LotAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("code", "vintage_year", "status", "allocated_lbs", "tons", "created_at")
    list_filter = ("status", "vintage_year")
    inlines = [DesignationInline, AllocationInline]

    @admin.display(description="allocated (lb)")
    def allocated_lbs(self, obj):
        return sum((a.allocated_net_lbs for a in obj.allocations.filter(voided_at__isnull=True)), 0)

    @admin.display(description="tons")
    def tons(self, obj):
        return round(float(self.allocated_lbs(obj)) / 2000.0, 3)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = LotAddForm
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["vintage_year", "gen_variety", "gen_program",
                    "gen_block", "gen_vineyard", "status", "production_intent"]
        return ["vintage_year", "status", "production_intent"]

    def get_inline_instances(self, request, obj=None):
        instances = super().get_inline_instances(request, obj)
        if obj is None:  # hide designation history on the add screen
            instances = [i for i in instances if not isinstance(i, DesignationInline)]
        return instances

    def get_changeform_initial_data(self, request):
        return {"vintage_year": date.today().year}

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)   # creates the Lot row
        if not change:
            assign_initial_designation(
                obj,
                variety=form.cleaned_data["gen_variety"],
                program=form.cleaned_data["gen_program"],
                block=form.cleaned_data.get("gen_block"),
                vineyard=form.cleaned_data.get("gen_vineyard"),
            )


@admin.register(WeighTag)
class WeighTagAdmin(AuditMixin, admin.ModelAdmin):
    inlines = [BinInline]
    list_display = ("weigh_tag_number", "harvest_event", "disposition",
                    "gross_total", "net_total", "allocated", "remaining", "alloc_status")
    list_filter = (AllocationStatusFilter, "disposition")

    @admin.display(description="gross (total)")
    def gross_total(self, obj):
        return obj.gross_total

    @admin.display(description="net (total)")
    def net_total(self, obj):
        return obj.net_total

    @admin.display(description="allocated")
    def allocated(self, obj):
        return obj.allocated_lbs

    @admin.display(description="remaining")
    def remaining(self, obj):
        return obj.remaining_lbs

    @admin.display(description="status")
    def alloc_status(self, obj):
        return {"unallocated": "⚠ Unallocated", "partial": "◐ Partial",
                "full": "✓ Fully allocated"}[_alloc_status(obj.net_total, obj.allocated_lbs)]


@admin.register(VarietalDesignation)
class DesignationAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("abbreviation", "variety", "program", "block", "vineyard", "is_curated")
    list_filter = ("program", "is_curated")


@admin.register(Vineyard)
class VineyardAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("name", "grower", "crush_district")
    list_filter = ("crush_district",)


for m in (Variety, Grower, Block, Vessel, Additive, LabAnalyte, LabAnalyteSynonym,
          ConfigConstant, HarvestEvent, HighProofSpiritLedger):
    admin.site.register(m)


# ------------------------------------------------------ fermentation ledger
class LabValueInline(admin.TabularInline):
    model = LabResultValue
    fields = ("analyte", "value", "qualifier", "flag", "display", "raw_result")
    extra = 3
    can_delete = False


@admin.register(LabResult)
class LabResultAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "reported_at", "source", "panel")
    list_filter = ("source", "panel", "lot")
    inlines = [LabValueInline]


@admin.register(Reading)
class ReadingAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "analyte", "value", "measured_at")
    list_filter = ("analyte", "lot")
    date_hierarchy = "measured_at"


@admin.register(Addition)
class AdditionAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "additive", "target", "computed_dose", "added_at")
    list_filter = ("additive", "lot")


@admin.register(DestemmingEvent)
class DestemmingAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "destem_at", "processing_path", "crusher_enabled", "fruit_condition")
    list_filter = ("processing_path", "lot")


@admin.register(TankAssignment)
class TankAssignmentAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "vessel", "assigned_at")
    list_filter = ("vessel", "lot")


for _model, _fields, _filters in [
    (ColdSoakSchedule, ("lot", "start_at", "target_inoc_date", "skipped"), ("lot",)),
    (PumpOverEvent, ("lot", "vessel", "started_at", "duration_min"), ("lot",)),
    (PunchDownEvent, ("lot", "vessel", "occurred_at", "foot_tread"), ("lot",)),
    (InoculationEvent, ("lot", "inoculated_at", "native", "yeast_strain"), ("lot",)),
    (LabRequest, ("lot", "sample_pulled_at", "panel_type"), ("lot",)),
    (CellarNote, ("lot", "noted_at", "body"), ("lot",)),
]:
    admin.site.register(
        _model,
        type(f"{_model.__name__}Admin", (AuditMixin, admin.ModelAdmin),
             {"list_display": _fields, "list_filter": _filters}),
    )


# ------------------------------------------------------------ crush-out
@admin.register(VolumeMeasurement)
class VolumeMeasurementAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "method", "volume_gal", "confidence", "is_booking_volume", "measured_at")
    list_filter = ("method", "is_booking_volume", "lot")

    @admin.display(description="confidence")
    def confidence(self, obj):
        return obj.confidence


@admin.register(PressingEvent)
class PressingAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "pressed_at", "free_run_gal", "press_gal", "disposition")
    list_filter = ("lot",)


@admin.register(FortificationEvent)
class FortificationAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "fortified_on_skins_date", "booked_at", "proof_gallons_drawn",
                    "spirit_wg", "finished_wg", "base_wg", "expected_tax_class", "part_x")
    list_filter = ("expected_tax_class", "lot")
    readonly_fields = ("spirit_wg", "base_wg", "hpgs_draw", "posting", "part_x", "yield_pct")
    fields = ("lot", "fortified_on_skins_date", "booked_at", "spirit_proof",
              "proof_gallons_drawn", "finished_wg", "expected_tax_class",
              "spirit_wg", "base_wg", "hpgs_draw", "posting", "part_x", "yield_pct")

    @admin.display(boolean=True, description="needs Part X")
    def part_x(self, obj):
        return obj.needs_part_x

    @admin.display(description="5120.17 posting")
    def posting(self, obj):
        if not obj.pk:
            return "(saved on create)"
        return " | ".join(f"{k}: {v}" for k, v in obj.form_5120_17_lines().items())

    @admin.display(description="yield check (% vs crush est.)")
    def yield_pct(self, obj):
        if not obj.pk:
            return ""
        y = obj.yield_check()
        return f"{y:+.1f}%" if y is not None else "n/a"


@admin.register(BookToBond)
class BookToBondAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "booked_at", "gallons_produced", "tax_class")
    list_filter = ("tax_class", "lot")


# ---------------------------------------------------------------- aging
from cellar.models import (
    BarrelOrder, Container, Rack, RackAssignment, AgingPlacement,
    VolumeLoss, ToppingEvent, ToppingTarget, Room, Location,
)
from cellar.services.aging import oak_summary, composition_report


class FlaggedBarrelFilter(admin.SimpleListFilter):
    title = "foreign-topping flag"
    parameter_name = "flagged"

    def lookups(self, request, model_admin):
        return [("yes", "⚠ Flagged (>5 gal foreign)")]

    def queryset(self, request, qs):
        if self.value() == "yes":
            return qs.filter(pk__in=[p.pk for p in qs if p.is_flagged])
        return qs


@admin.register(Container)
class ContainerAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("container_id", "type", "format", "capacity_gal", "forest", "toast",
                    "fill_count", "rack_loc", "location", "landed_usd")
    list_filter = ("type", "forest", "toast")
    search_fields = ("container_id", "barcode")

    @admin.display(description="fills")
    def fill_count(self, obj):
        return obj.fill_count

    @admin.display(description="rack")
    def rack_loc(self, obj):
        a = obj.current_rack_assignment()
        return f"{a.rack.rack_id}·{a.position}" if a else "—"

    @admin.display(description="location")
    def location(self, obj):
        loc = obj.effective_location()
        return loc.code if loc else "—"

    @admin.display(description="landed $")
    def landed_usd(self, obj):
        v = obj.landed_cost_usd()
        return f"${v}" if v is not None else "—"


@admin.register(Rack)
class RackAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("rack_id", "location", "positions", "occupant_summary", "split_flag")
    list_filter = ("location__room", "location")
    list_editable = ("location",)   # batch-assign racks to a location on one screen
    search_fields = ("rack_id", "barcode")

    @admin.display(description="occupants")
    def occupant_summary(self, obj):
        occ = obj.occupants()
        return ", ".join(f"{pos}:{c.container_id}" for pos, c in sorted(occ.items())) or "empty"

    @admin.display(boolean=True, description="⚠ split")
    def split_flag(self, obj):
        return obj.is_split


@admin.register(Location)
class LocationAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("code", "room", "rack_count")
    list_filter = ("room",)

    @admin.display(description="racks")
    def rack_count(self, obj):
        return obj.racks.count()


admin.site.register(Room)


@admin.register(AgingPlacement)
class PlacementAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "container", "volume_gal", "oak_tier", "fill_number",
                    "filled_at", "emptied_at", "flag")
    list_filter = (FlaggedBarrelFilter, "oak_tier", "lot")

    @admin.display(boolean=True, description="⚠ foreign")
    def flag(self, obj):
        return obj.is_flagged


class ToppingTargetInline(admin.TabularInline):
    model = ToppingTarget
    fields = ("placement", "volume_added")
    extra = 3
    can_delete = False


@admin.register(ToppingEvent)
class ToppingAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("source_lot", "kind", "topped_at")
    list_filter = ("kind", "source_lot")
    inlines = [ToppingTargetInline]


@admin.register(BarrelOrder)
class BarrelOrderAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("supplier", "order_date", "currency", "fx_rate_to_usd", "bank_fee", "delivery_fee")


admin.site.register(RackAssignment)
admin.site.register(VolumeLoss)

admin.site.add_action(void_entries)


# ---------------------------------------------------------------- bottling
from cellar.models import (BottleFormat, DryGood, BottlingRun, BottlingDryGoodUse, TaxPaidRemoval)
from cellar.services.costing import bottling_cogs
from cellar.services.aging import composition_report


class DryGoodUseInline(admin.TabularInline):
    model = BottlingDryGoodUse
    fields = ("dry_good", "quantity")
    extra = 3
    can_delete = False


class RemovalInline(admin.TabularInline):
    model = TaxPaidRemoval
    fields = ("removed_at", "cases", "channel")
    extra = 1
    can_delete = False


@admin.register(BottlingRun)
class BottlingRunAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("sku", "bottled_at", "source_lot", "bottle_format", "cases_produced",
                    "bottles_produced", "loss", "cases_on_hand")
    list_filter = ("bottle_format", "source_lot")
    inlines = [DryGoodUseInline, RemovalInline]
    readonly_fields = ("bottles_produced", "volume_bottled_gal", "loss", "label_composition", "cogs")

    @admin.display(description="bottling loss (gal)")
    def loss(self, obj):
        return obj.bottling_loss_gal

    @admin.display(description="label composition (leaf lots)")
    def label_composition(self, obj):
        if not obj.pk:
            return ""
        return " | ".join(f"{k}: {v}%" for k, v in composition_report(obj.source_lot).items())

    @admin.display(description="COGS")
    def cogs(self, obj):
        if not obj.pk:
            return ""
        c = bottling_cogs(obj)
        return (f"wine ${c['wine_cost']} + dry ${c['dry_goods_cost']} + line ${c['line_labor_cost']} "
                f"= ${c['total_cogs']}  →  ${c['cost_per_bottle']}/btl, ${c['cost_per_case']}/cs")


@admin.register(BottleFormat)
class BottleFormatAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("name", "ml", "bottles_per_case", "gal_per_bottle", "case_gallons")


@admin.register(DryGood)
class DryGoodAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("name", "kind", "unit_cost", "unit")
    list_filter = ("kind",)


@admin.register(TaxPaidRemoval)
class TaxPaidRemovalAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("bottling_run", "removed_at", "cases", "bottles", "wine_gallons_removed", "channel")
    list_filter = ("channel",)


# ---------------------------------------------------------------- reporting
from cellar.models import (BondTransfer, Material, MaterialTransaction, SweeteningEvent, BondAdjustment)

@admin.register(BondTransfer)
class BondTransferAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("transferred_at", "direction", "phase", "tax_class", "gallons", "counterparty")
    list_filter = ("direction", "phase", "tax_class")

@admin.register(SweeteningEvent)
class SweeteningAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("lot", "sweetened_at", "tax_class", "volume_used", "concentrate_gallons", "volume_produced")
    list_filter = ("tax_class",)
    readonly_fields = ("volume_produced", "material_use")

@admin.register(BondAdjustment)
class BondAdjustmentAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("occurred_at", "kind", "phase", "tax_class", "gallons")
    list_filter = ("kind", "phase")

@admin.register(MaterialTransaction)
class MaterialTxnAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("occurred_at", "direction", "material", "quantity")
    list_filter = ("direction", "material")

admin.site.register(Material)


from cellar.models import BulkTaxPaidRemoval
@admin.register(BulkTaxPaidRemoval)
class BulkTaxPaidRemovalAdmin(AuditMixin, admin.ModelAdmin):
    list_display = ("removed_at", "lot", "tax_class", "wine_gallons", "channel")
    list_filter = ("tax_class", "channel")


# ------------------------------------------------------------------- tasks
class TaskEventInline(admin.TabularInline):
    model = TaskEvent
    fields = ("kind", "detail", "operator", "created_at")
    readonly_fields = ("created_at",)
    extra = 0
    can_delete = False


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "assignee", "due_date", "lot", "rule")
    list_filter = ("status", "assignee", "rule")
    search_fields = ("title", "body")
    inlines = [TaskEventInline]


@admin.register(TaskRule)
class TaskRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "enabled")
    list_editable = ("enabled",)
