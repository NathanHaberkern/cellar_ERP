"""
Reference editors — generic CRUD for the master-data tables that only had a
Django-admin UI before (Varieties, the VarietalDesignation catalog, growers/
vineyards/blocks, vessels, barrels/racks/containers, bottle formats, dry
goods, lab analytes, config constants).

Additives (views.py) proved the pattern: a table + an inline add form, straight
create/update since these are editable masters, not append-only ledger rows.
Replicating that by hand for fourteen more tables would be fourteen more
templates saying the same thing. This module does it once, driven by a small
per-table spec (`REGISTRY`) that names the model, its editable fields, and any
FK querysets — Django's ModelForm generates the right widget for each field
type (text, number, select, checkbox) from the model itself, so a field never
needs to be hand-described twice.

Adding a new reference table means adding one entry to REGISTRY, not a new
view/template pair.
"""
from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from cellar.models import (
    Variety, Grower, Vineyard, Block, VarietalDesignation, Vessel,
    LabAnalyte, LabAnalyteSynonym, ConfigConstant,
    Room, Location, BarrelOrder, Container, Rack,
    BottleFormat, DryGood, Material, ExternalDestination,
    FruitPrice, FruitPriceRevision,
    TankAssignment,
)


class TableSpec:
    """One reference table's editor config.

    slug       : URL segment, e.g. 'varieties'
    model      : the Django model
    label      : plural display name
    fields     : field names to show as columns / form inputs, in order
    order_by   : queryset ordering
    row_filter : optional (qs, request) -> qs hook for tables that need a
                 default filter with a toggle (e.g. hiding emptied bin vessels)
    """
    def __init__(self, slug, model, label, fields, order_by=None, row_filter=None):
        self.slug = slug
        self.model = model
        self.label = label
        self.fields = fields
        self.order_by = order_by or fields[:1]
        self.row_filter = row_filter

    def form_class(self):
        spec = self

        class _Form(forms.ModelForm):
            class Meta:
                model = spec.model
                fields = spec.fields
        return _Form

    def queryset(self, request=None):
        qs = self.model.objects.order_by(*self.order_by)
        if self.row_filter:
            qs = self.row_filter(qs, request)
        return qs


# Macro-bin / 1-ton-bin vessels are created fresh per crush (see
# services/operations.py) and are meaningless clutter once their lot has moved
# on. A vessel is "current" if it has an open (unvacated) TankAssignment; real
# tanks are never filtered regardless of occupancy. `?show_empty_bins=1`
# reveals them — same "hide by default, toggle to see" pattern as the ledger's
# hide_voided checkbox.
def _vessel_row_filter(qs, request):
    if request and request.GET.get("show_empty_bins") == "1":
        return qs
    bin_types = (Vessel.Type.MACRO_BIN, Vessel.Type.ONE_TON_BIN)
    open_bin_ids = TankAssignment.objects.filter(
        voided_at__isnull=True, emptied_at__isnull=True,
        vessel__type__in=bin_types,
    ).values_list("vessel_id", flat=True)
    from django.db.models import Q
    return qs.exclude(Q(type__in=bin_types) & ~Q(pk__in=open_bin_ids))


# ----------------------------------------------------------------- registry
REGISTRY = {
    t.slug: t for t in [
        TableSpec("varieties", Variety, "Varieties",
                  ["name", "notes"]),
        TableSpec("growers", Grower, "Growers",
                  ["name", "source_type"]),
        TableSpec("vineyards", Vineyard, "Vineyards",
                  ["grower", "name", "crush_district", "crush_report_district"]),
        TableSpec("blocks", Block, "Blocks",
                  ["vineyard", "variety", "name", "acreage"]),
        TableSpec("designations", VarietalDesignation, "Varietal designations",
                  ["variety", "program", "abbreviation", "block", "vineyard", "is_curated"]),
        TableSpec("vessels", Vessel, "Vessels",
                  ["code", "type", "capacity_gal", "max_fruit_tons", "refrigerated",
                   "temp_controlled", "volume_method", "room"],
                  row_filter=_vessel_row_filter),
        TableSpec("rooms", Room, "Rooms",
                  ["name", "notes"]),
        TableSpec("locations", Location, "Locations",
                  ["room", "code"]),
        TableSpec("racks", Rack, "Racks",
                  ["rack_id", "location", "positions", "barcode"]),
        TableSpec("barrel-orders", BarrelOrder, "Barrel orders",
                  ["supplier", "order_date", "currency", "fx_rate_to_usd",
                   "bank_fee", "delivery_fee"], order_by=["-order_date"]),
        TableSpec("containers", Container, "Barrels / containers",
                  ["container_id", "type", "capacity_gal", "active", "format",
                   "origin", "forest", "cooper", "toast", "head_toast", "grain",
                   "year_made", "order", "base_price"]),
        TableSpec("bottle-formats", BottleFormat, "Bottle formats",
                  ["name", "ml", "bottles_per_case"]),
        TableSpec("dry-goods", DryGood, "Dry goods",
                  ["name", "kind", "unit_cost", "unit"]),
        TableSpec("analytes", LabAnalyte, "Lab analytes",
                  ["name", "slug", "unit", "in_house", "sort_order"]),
        TableSpec("analyte-synonyms", LabAnalyteSynonym, "Lab analyte synonyms",
                  ["raw_name", "analyte"]),
        TableSpec("config-constants", ConfigConstant, "Config constants",
                  ["key", "value", "unit", "notes"]),
        TableSpec("materials", Material, "Materials",
                  ["name", "kind", "unit", "unit_cost"]),
        TableSpec("external-destinations", ExternalDestination, "External destinations",
                  ["name", "bw_number", "notes"]),
        TableSpec("fruit-prices", FruitPrice, "Fruit prices",
                  ["vintage_year", "variety", "block", "price_per_ton", "basis",
                   "source_ref", "is_provisional", "notes"],
                  order_by=["-vintage_year", "variety__name"]),
        TableSpec("fruit-price-revisions", FruitPriceRevision, "Fruit price revisions",
                  ["price", "final_price_per_ton", "basis", "source_ref", "effective_on"],
                  order_by=["-effective_on"]),
    ]
}


def _display_row(spec, obj):
    """[(field_name, display_value)] for one object — resolves FKs and choice
    fields to their human string, like Django admin's list_display would."""
    out = []
    for fname in spec.fields:
        val = getattr(obj, fname, None)
        display_fn = getattr(obj, f"get_{fname}_display", None)
        if callable(display_fn):
            val = display_fn()
        elif hasattr(val, "pk"):   # FK — show its __str__
            val = str(val)
        elif val is True:
            val = "yes"
        elif val is False:
            val = "no"
        elif val in (None, ""):
            val = "—"
        out.append((fname, val))
    return out


@login_required
def reference_index(request):
    return render(request, "web/reference_index.html",
                  {"nav": "reference", "tables": REGISTRY.values()})


@login_required
def reference_table(request, slug):
    spec = get_object_or_404_spec(slug)
    Form = spec.form_class()
    rows = spec.queryset(request)
    return render(request, "web/reference_table.html", {
        "nav": "reference", "spec": spec, "tables": REGISTRY.values(),
        "row_objs": [(obj, _display_row(spec, obj)) for obj in rows],
        "form": Form(),
        "show_empty_bins": request.GET.get("show_empty_bins") == "1",
    })


@login_required
def reference_edit_row(request, slug, pk):
    """HTMX fragment: swap a display row for its inline edit form."""
    spec = get_object_or_404_spec(slug)
    obj = get_object_or_404(spec.model, pk=pk)
    Form = spec.form_class()
    return render(request, "web/_reference_row_edit.html",
                  {"spec": spec, "obj": obj, "form": Form(instance=obj)})


@login_required
@require_http_methods(["POST"])
def reference_create(request, slug):
    spec = get_object_or_404_spec(slug)
    Form = spec.form_class()
    form = Form(request.POST)
    if form.is_valid():
        obj = form.save()
        return render(request, "web/_reference_row.html",
                      {"spec": spec, "obj": obj, "display": _display_row(spec, obj)})
    return render(request, "web/_reference_row.html",
                  {"spec": spec, "obj": None, "form_errors": form.errors}, status=400)


@login_required
@require_http_methods(["POST"])
def reference_update(request, slug, pk):
    spec = get_object_or_404_spec(slug)
    obj = get_object_or_404(spec.model, pk=pk)
    Form = spec.form_class()
    form = Form(request.POST, instance=obj)
    if form.is_valid():
        obj = form.save()
        return render(request, "web/_reference_row.html",
                      {"spec": spec, "obj": obj, "display": _display_row(spec, obj)})
    return render(request, "web/_reference_row_edit.html",
                  {"spec": spec, "obj": obj, "form": form}, status=400)


def get_object_or_404_spec(slug):
    spec = REGISTRY.get(slug)
    if spec is None:
        from django.http import Http404
        raise Http404(f"Unknown reference table: {slug}")
    return spec
