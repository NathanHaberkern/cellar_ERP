from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),

    # JSON API (iOS later): token + session auth
    path("api/", include("cellar.api.urls")),

    # Shipping (C7 -> FedEx/UPS labels)
    path("shipping/", include("shipping.urls")),

    # Club desk (member requests -> approved C7 changes)
    path("club/", include("clubdesk.urls")),

    # HTMX front end (browser now): session auth, server-rendered
    path("", include("cellar.web.urls")),

    # Optional: DRF browsable-API login (dev convenience only)
    path("api-auth/", include("rest_framework.urls")),
]