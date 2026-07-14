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
from . import labs
from . import tasks
from . import fermentation as ferment
from . import bottling as bottle
from . import bonding as bond
from . import fortification as fort
from . import topping as top
from . import blend as blend_web
from . import reference as ref
from . import daily as daily_web
from . import weightags
from . import sweeten

urlpatterns = [
    # session auth (built-in Django views, our templates)
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html"),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),

    # app
    path("", views.dashboard, name="dashboard"),

    # daily checklist + plan
    path("daily/", daily_web.daily_index, name="daily"),
    path("daily/quick-log/<int:lot_pk>/", daily_web.daily_quick_log, name="daily-quick-log"),  # HTMX
    path("daily/<int:plan_pk>/toggle/<str:item_id>/", daily_web.daily_item_toggle, name="daily-item-toggle"),  # HTMX
    path("daily/<int:plan_pk>/add/", daily_web.daily_item_add, name="daily-item-add"),  # HTMX
    path("daily/<int:plan_pk>/remove/<str:item_id>/", daily_web.daily_item_remove, name="daily-item-remove"),  # HTMX
    path("daily/<int:plan_pk>/regenerate/", daily_web.daily_regenerate, name="daily-regenerate"),  # HTMX

    # guided receiving-fruit intake
    path("intake/", intake.intake_index, name="intake"),
    path("intake/estimate/", intake.intake_estimate, name="intake-estimate"),      # HTMX
    path("intake/tag-bins/", intake.intake_tag_bins, name="intake-tag-bins"),      # HTMX
    path("intake/destem/", intake.intake_destem, name="intake-destem"),            # HTMX
    path("intake/dose/", intake.dose_preview, name="intake-dose"),                 # HTMX
    path("intake/<int:lot_pk>/addition/", intake.intake_addition, name="intake-addition"),  # HTMX

    path("weigh-tags/", weightags.weightag_list, name="weightag-list"),
    path("weigh-tags/<int:pk>/", weightags.weightag_detail, name="weightag-detail"),

    path("lots/", views.lots_list, name="lots"),
    path("lots/search/", views.lots_search, name="lots-search"),          # HTMX
    path("lots/<int:pk>/", views.lot_detail, name="lot-detail"),
    # lot detail sub-panels (HTMX fragments swapped into #lot-panel)
    path("lots/<int:pk>/additions/", views.lot_additions, name="lot-additions"),
    path("lots/<int:pk>/labs/", views.lot_labs, name="lot-labs"),
    path("lots/<int:pk>/movement/", views.lot_movement, name="lot-movement"),
    path("lots/<int:pk>/composition/", views.lot_composition, name="lot-composition"),
    path("lots/<int:pk>/composition/override/", views.lot_composition_override_save, name="lot-composition-override"),  # HTMX
    path("lots/<int:pk>/oak/", views.lot_oak, name="lot-oak"),
    path("lots/<int:pk>/cost/", views.lot_cost, name="lot-cost"),
    path("lots/<int:pk>/tasks/", views.lot_tasks, name="lot-tasks"),
    # fermentation module (HTMX sub-panel, steps 1-4)
    path("lots/<int:pk>/ferment/", ferment.lot_ferment, name="lot-ferment"),
    path("lots/<int:pk>/ferment/preview/", ferment.ferment_preview, name="ferment-preview"),
    path("lots/<int:pk>/ferment/inoculate/", ferment.ferment_inoculate, name="ferment-inoculate"),
    path("lots/<int:pk>/ferment/daily/", ferment.ferment_daily, name="ferment-daily"),
    path("lots/<int:pk>/ferment/confirm/<int:task_pk>/", ferment.ferment_confirm, name="ferment-confirm"),
    path("lots/<int:pk>/ferment/press-first/", ferment.ferment_press_first, name="ferment-press-first"),
    path("lots/<int:pk>/ferment/rack-lees/", ferment.ferment_rack_lees, name="ferment-rack-lees"),
    path("lots/<int:pk>/ferment/press/", ferment.ferment_press, name="ferment-press"),
    # ferment-rack RETIRED — racking to barrel moved to the Oak tab (lot-rack-to-barrel);
    # it is an aging move and no longer ends primary. Book-to-bond does that now.
    # book-to-bond (the declaration that ends primary) + barrel-down (aging)
    path("lots/<int:pk>/bond/", bond.lot_bond_card, name="lot-bond-card"),
    path("lots/<int:pk>/bond/book/", bond.lot_book_to_bond, name="lot-book-to-bond"),
    path("lots/<int:pk>/oak/rack/", bond.lot_rack_to_barrel, name="lot-rack-to-barrel"),
    path("lots/<int:pk>/oak/barrel-search/", bond.oak_barrel_search, name="oak-barrel-search"),  # HTMX
    path("lots/<int:pk>/oak/top/", top.lot_top_barrels, name="lot-top-barrels"),
    path("lots/<int:pk>/oak/rack-out/", top.lot_rack_out, name="lot-rack-out"),

    # blending (on the Movement tab)
    path("lots/<int:pk>/blend/preview/", blend_web.blend_preview, name="blend-preview"),  # HTMX
    path("lots/<int:pk>/blend/commit/", blend_web.lot_blend_commit, name="lot-blend-commit"),

    # fortification / Port (own tab; Port-designated lots only)
    path("lots/<int:pk>/fortification/", fort.lot_fortification, name="lot-fortification"),
    path("lots/<int:pk>/fortification/preview/", fort.fortification_preview, name="fortification-preview"),  # HTMX
    path("lots/<int:pk>/fortification/initial/", fort.lot_fortify_initial, name="lot-fortify-initial"),
    path("lots/<int:pk>/fortification/adjust/", fort.lot_fortify_adjust, name="lot-fortify-adjust"),

    # bottling (parcel split + run)
    path("lots/<int:pk>/bottling/", bottle.lot_bottling, name="lot-bottling"),
    path("lots/<int:pk>/bottling/prepare/", bottle.bottling_prepare, name="bottling-prepare"),
    path("lots/<int:pk>/bottling/run/", bottle.bottling_run, name="bottling-run"),
    # section scratchpad note + on-page entry actions
    path("lots/<int:pk>/note/<slug:section>/", views.lot_note_save, name="lot-note-save"),
    path("lots/<int:pk>/ferment/skin-contact-override/", views.lot_skin_contact_override_save,
         name="lot-skin-contact-override"),  # HTMX
    path("lots/<int:pk>/additions/add/", views.lot_addition_create, name="lot-addition-create"),
    path("lots/<int:pk>/labs/add/", views.lot_lab_create, name="lot-lab-create"),
    path("lots/<int:pk>/movement/transfer/", views.lot_transfer_create, name="lot-transfer-create"),
    path("lots/<int:pk>/movement/split/", views.lot_split_create, name="lot-split-create"),
    path("lots/<int:pk>/movement/external-transfer/", views.lot_external_transfer_create, name="lot-external-transfer-create"),

    # back-sweetening (own tab)
    path("lots/<int:pk>/sweeten/", sweeten.lot_sweeten, name="lot-sweeten"),
    path("lots/<int:pk>/sweeten/preview/", sweeten.sweeten_preview, name="lot-sweeten-preview"),  # HTMX
    path("lots/<int:pk>/sweeten/create/", sweeten.lot_sweeten_create, name="lot-sweeten-create"),

    # lab CSV import (ETS)
    path("labs/import/", labs.labs_import_index, name="labs-import"),
    path("labs/import/preview/", labs.labs_import_preview, name="labs-import-preview"),  # HTMX
    path("labs/import/commit/", labs.labs_import_commit, name="labs-import-commit"),      # HTMX

    # tasks
    path("tasks/", tasks.dash_tasks, name="dash-tasks"),                          # HTMX (dashboard list)
    path("tasks/<int:pk>/action/", tasks.task_action, name="task-action"),        # HTMX
    path("tasks/<int:pk>/reassign/", tasks.task_reassign, name="task-reassign"),  # HTMX
    path("lots/<int:pk>/tasks/add/", tasks.lot_task_create, name="lot-task-create"),  # HTMX
    path("rules/", tasks.rules_index, name="rules"),
    path("rules/<int:pk>/update/", tasks.rule_update, name="rule-update"),        # HTMX

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

    # generic reference table editors
    path("reference/", ref.reference_index, name="reference-index"),
    path("reference/<slug:slug>/", ref.reference_table, name="reference-table"),
    path("reference/<slug:slug>/create/", ref.reference_create, name="reference-create"),  # HTMX
    path("reference/<slug:slug>/<int:pk>/edit/", ref.reference_edit_row, name="reference-edit-row"),  # HTMX
    path("reference/<slug:slug>/<int:pk>/update/", ref.reference_update, name="reference-update"),  # HTMX
]
