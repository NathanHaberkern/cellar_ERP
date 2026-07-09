"""
HTMX front-end routes. Mounted at site root (see config_patches).
Auth uses Django's built-in session login views -- same session the API's
SessionAuthentication reads, so a signed-in browser is authenticated for both.
"""

from django.urls import path
from django.contrib.auth import views as auth_views

from . import views
from . import ledger
from . import scan
from . import intake

urlpatterns = [
    # session auth (built-in Django views, our templates)
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html"),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),

    # app
    path("", views.dashboard, name="dashboard"),

    # guided receiving-fruit intake
    path("intake/", intake.intake_index, name="intake"),
    path("intake/estimate/", intake.intake_estimate, name="intake-estimate"),      # HTMX
    path("intake/destem/", intake.intake_destem, name="intake-destem"),            # HTMX
    path("intake/dose/", intake.dose_preview, name="intake-dose"),                 # HTMX
    path("intake/<int:lot_pk>/addition/", intake.intake_addition, name="intake-addition"),  # HTMX

    path("lots/", views.lots_list, name="lots"),
    path("lots/search/", views.lots_search, name="lots-search"),          # HTMX
    path("lots/<int:pk>/", views.lot_detail, name="lot-detail"),
    path("lots/<int:pk>/composition/", views.lot_composition, name="lot-composition"),  # HTMX
    path("lots/<int:pk>/oak/", views.lot_oak, name="lot-oak"),            # HTMX
    path("lots/<int:pk>/cost/", views.lot_cost, name="lot-cost"),        # HTMX

    path("reports/", views.reports_index, name="reports"),
    path("reports/run/", views.report_run, name="report-run"),           # HTMX

    # barcode scan-to-move
    path("move/", scan.scan_index, name="move"),
    path("move/resolve/container/", scan.resolve_container, name="scan-resolve-container"),  # HTMX
    path("move/resolve/rack/", scan.resolve_rack, name="scan-resolve-rack"),                 # HTMX
    path("move/book/", scan.book_move, name="scan-book"),                                    # HTMX

    # append-only ledger (generic viewer + void/close)
    path("ledger/", ledger.ledger_index, name="ledger"),
    path("ledger/<slug:slug>/", ledger.ledger_list, name="ledger-list"),
    path("ledger/<slug:slug>/rows/", ledger.ledger_rows, name="ledger-rows"),          # HTMX
    path("ledger/<slug:slug>/<int:pk>/void/", ledger.ledger_void, name="ledger-void"),  # HTMX
    path("ledger/<slug:slug>/<int:pk>/close/", ledger.ledger_close, name="ledger-close"),# HTMX

    path("reference/additives/", views.additives, name="additives"),
    path("reference/additives/create/", views.additive_create, name="additive-create"),  # HTMX
    path("reference/additives/<int:pk>/update/", views.additive_update, name="additive-update"),  # HTMX
]
