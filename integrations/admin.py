from django.contrib import admin

from .models import QBOConnection


@admin.register(QBOConnection)
class QBOConnectionAdmin(admin.ModelAdmin):
    list_display = ("environment", "company_name", "realm_id", "updated_at", "refresh_expires_at")
    # Tokens are secrets: never shown or edited here. Reconnect from /integrations/qbo/.
    exclude = ("refresh_token", "access_token")
    readonly_fields = ("environment", "realm_id", "company_name", "refresh_expires_at",
                       "access_expires_at", "connected_at", "updated_at")
