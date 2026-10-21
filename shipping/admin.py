"""Admin for shipping reference data, plus read-only views of carrier records.

Box sizes, bottle weights and SKU overrides are editable here (that's the
Tranche 1 UI for them). Labels and fulfillment posts are read-only: they are
created by the services, and a label is only ever voided through
`void_label`, which cancels it at the carrier first.
"""
from django.contrib import admin

from .models import (BottleWeight, BoxSize, FulfillmentPost, Label, LabelDocument, Package,
                     PackPool, ShipOrder, ShipOrderLine, SkuShippingProfile)


@admin.register(BoxSize)
class BoxSizeAdmin(admin.ModelAdmin):
    list_display = ("name", "use", "dimensions_display", "tare_lb", "max_bottles",
                    "max_weight_lb", "active", "sort_order")
    list_editable = ("active", "sort_order")
    list_filter = ("use", "active")


@admin.register(BottleWeight)
class BottleWeightAdmin(admin.ModelAdmin):
    list_display = ("label", "ml", "weight_lb")


@admin.register(SkuShippingProfile)
class SkuShippingProfileAdmin(admin.ModelAdmin):
    list_display = ("sku", "ml", "bottles_per_unit", "notes")
    search_fields = ("sku", "notes")


class ShipOrderLineInline(admin.TabularInline):
    model = ShipOrderLine
    extra = 0


@admin.register(ShipOrder)
class ShipOrderAdmin(admin.ModelAdmin):
    list_display = ("c7_order_number", "ship_name", "ship_state", "status", "carrier",
                    "is_club", "ordered_at")
    list_filter = ("status", "carrier", "is_club", "ship_state")
    search_fields = ("c7_order_number", "ship_name", "ship_email", "ship_postal_code")
    readonly_fields = ("c7_payload", "synced_at", "created_at")
    inlines = [ShipOrderLineInline]


@admin.register(PackPool)
class PackPoolAdmin(admin.ModelAdmin):
    list_display = ("name", "carrier", "status", "created_at", "created_by")
    list_filter = ("carrier", "status")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("pk", "pool", "box", "bottle_count", "weight_lb", "created_at")
    list_filter = ("box",)
    filter_horizontal = ("orders",)


class LabelDocumentInline(admin.TabularInline):
    model = LabelDocument
    fields = ("sequence", "kind", "content_type")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Label)
class LabelAdmin(ReadOnlyAdmin):
    list_display = ("tracking_number", "carrier", "environment", "service", "weight_lb",
                    "net_charge", "created_at", "voided_at")
    list_filter = ("carrier", "environment", "service")
    search_fields = ("tracking_number", "master_tracking_number")
    inlines = [LabelDocumentInline]


@admin.register(FulfillmentPost)
class FulfillmentPostAdmin(ReadOnlyAdmin):
    list_display = ("order", "carrier", "succeeded", "http_status", "created_at")
    list_filter = ("succeeded", "carrier")
