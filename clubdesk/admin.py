from django.contrib import admin

from .models import C7Write, ClubRequest


@admin.register(ClubRequest)
class ClubRequestAdmin(admin.ModelAdmin):
    list_display = ("received_at", "kind", "customer_name", "customer_email", "status", "approved_by")
    list_filter = ("status", "kind", "mailbox")
    search_fields = ("customer_email", "customer_name")
    readonly_fields = ("approved_by", "approved_at", "applied_at")


@admin.register(C7Write)
class C7WriteAdmin(admin.ModelAdmin):
    list_display = ("made_at", "request", "method", "path", "status_code", "ok", "is_note")
    list_filter = ("ok", "is_note", "method")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
