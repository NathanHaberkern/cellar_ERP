"""
Append-only ledger front end (the AppendOnly variant of the reference pattern).

Reference masters (Additives etc.) are editable -> create/update in place.
Ledger/event rows are NOT: insert-only, never edited or deleted. Corrections are
made exactly as in admin -- VOID the row (it stays in the ledger, struck through)
and add a new one. Some temporal rows also expose CLOSE_FIELDS (e.g. emptied_at,
removed_at) that may be set once after creation; those get a "Close" control.

This is ONE generic viewer parameterized by a registry, not a bespoke screen per
model -- so it already covers every append-only table and new ones are a one-line
add to LEDGER. Columns are auto-derived from the model, so no field names are
hard-coded here.

Voiding mirrors admin.void_entries (a bulk update of voided_at); closing a
CLOSE_FIELD is the same bulk-update pattern. Columns auto-derive from each model
(minus the AppendOnly plumbing), so no field names are hard-coded.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

# Append-only event/ledger models, per the documented module map. Each entry:
#   slug: (Human label, model, columns or None=auto-derive)
# Add a table by adding a line. Confidently-append-only, compliance-relevant
# events a GM reviews are registered first; extend freely.
from cellar.models.ledger import Reading, Addition
from cellar.models.crushout import FortificationEvent, PressingEvent
from cellar.models.aging import ToppingEvent, VolumeLoss, RackAssignment
from cellar.models.bottling import BottlingRun, TaxPaidRemoval

LEDGER = {
    "additions":        ("Additions",          Addition,          None),
    "readings":         ("Readings",           Reading,           None),
    "fortifications":   ("Fortifications",     FortificationEvent, None),
    "pressings":        ("Pressings",          PressingEvent,     None),
    "rack-assignments": ("Rack moves",          RackAssignment,    None),
    "toppings":         ("Topping events",     ToppingEvent,      None),
    "volume-losses":    ("Volume losses",      VolumeLoss,        None),
    "bottling-runs":    ("Bottling runs",      BottlingRun,       None),
    "tax-paid-removals":("Tax-paid removals",  TaxPaidRemoval,    None),
}


# ---------------------------------------------------------------- helpers ----
def _entry(slug):
    if slug not in LEDGER:
        raise Http404("Unknown ledger.")
    return LEDGER[slug]


def _columns(model, configured):
    """Display columns: registry-configured, else the model's own concrete
    fields (minus pk and the void marker), capped so the table stays scannable."""
    if configured:
        return list(configured)
    # hide the AppendOnly plumbing; keep created_at + the model's own fields
    skip = {"id", "voided_at", "supersedes", "operator", "notes"}
    names = [f.name for f in model._meta.concrete_fields if f.name not in skip]
    return names[:6]


def _cell(obj, name):
    """One display value, formatted, no field knowledge required."""
    val = getattr(obj, name, None)
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if hasattr(val, "isoformat"):  # date / datetime
        try:
            val = timezone.localtime(val)
        except Exception:
            pass
        return val.strftime("%Y-%m-%d %H:%M") if hasattr(val, "hour") else val.isoformat()
    return str(val)


def _close_fields(model):
    """CLOSE_FIELDS declared on the AppendOnly model (settable once after
    creation). Empty/absent -> no close control."""
    return list(getattr(model, "CLOSE_FIELDS", []) or [])


def _row(obj, model, columns, close_fields):
    voided = getattr(obj, "voided_at", None) is not None
    # a close field is "open" if it exists on the model and isn't set yet
    open_closes = [f for f in close_fields if getattr(obj, f, None) is None] if not voided else []
    return {
        "pk": obj.pk,
        "cells": [_cell(obj, c) for c in columns],
        "voided": voided,
        "voided_at": _cell(obj, "voided_at") if voided else "",
        "open_closes": open_closes,
    }


def _has_void(model):
    """Whether this model supports voiding at all (method or field)."""
    if hasattr(model, "void"):
        return True
    return any(f.name == "voided_at" for f in model._meta.concrete_fields)


def _void_instance(obj):
    # Mirrors admin.void_entries: a bulk update of voided_at (append-only correction).
    type(obj).objects.filter(pk=obj.pk).update(voided_at=timezone.now())


def _render_list(request, slug, *, fragment):
    label, model, configured = _entry(slug)
    columns = _columns(model, configured)
    close_fields = _close_fields(model)
    qs = model.objects.all().order_by("-pk")
    hide_voided = request.GET.get("hide_voided") == "1"
    if hide_voided and _has_void(model):
        qs = qs.filter(voided_at__isnull=True)
    rows = [_row(o, model, columns, close_fields) for o in qs[:300]]
    ctx = {
        "nav": "ledger", "slug": slug, "label": label,
        "headers": [c.replace("_", " ").title() for c in columns],
        "columns": columns, "rows": rows,
        "can_void": _has_void(model), "close_fields": close_fields,
        "hide_voided": hide_voided,
    }
    tpl = "web/_ledger_rows.html" if fragment else "web/ledger_list.html"
    return render(request, tpl, ctx)


# ------------------------------------------------------------------ views ----
@login_required
def ledger_index(request):
    items = []
    for slug, (label, model, _cfg) in LEDGER.items():
        total = model.objects.count()
        items.append({"slug": slug, "label": label, "count": total,
                      "voidable": _has_void(model)})
    return render(request, "web/ledger_index.html", {"nav": "ledger", "items": items})


@login_required
def ledger_list(request, slug):
    return _render_list(request, slug, fragment=False)


@login_required
def ledger_rows(request, slug):
    # HTMX fragment: tbody only (used by the "hide voided" toggle)
    return _render_list(request, slug, fragment=True)


@login_required
@require_http_methods(["POST"])
def ledger_void(request, slug, pk):
    label, model, configured = _entry(slug)
    obj = get_object_or_404(model, pk=pk)
    columns = _columns(model, configured)
    close_fields = _close_fields(model)
    error = None
    try:
        _void_instance(obj)
        obj.refresh_from_db()
    except Exception as e:  # noqa: BLE001
        error = f"{model.__name__}.void: {e}"
    return render(request, "web/_ledger_row.html", {
        "slug": slug, "row": _row(obj, model, columns, close_fields),
        "can_void": _has_void(model), "error": error,
        "ncols": len(columns),
    })


@login_required
@require_http_methods(["POST"])
def ledger_close(request, slug, pk):
    label, model, configured = _entry(slug)
    obj = get_object_or_404(model, pk=pk)
    columns = _columns(model, configured)
    close_fields = _close_fields(model)
    field = request.POST.get("field")
    error = None
    if field not in close_fields:
        error = f"'{field}' is not a close field on {model.__name__}."
    else:
        try:
            model.objects.filter(pk=obj.pk).update(**{field: timezone.now()})
            obj.refresh_from_db()
        except Exception as e:  # noqa: BLE001
            error = f"{model.__name__}.{field}: {e}"
    return render(request, "web/_ledger_row.html", {
        "slug": slug, "row": _row(obj, model, columns, close_fields),
        "can_void": _has_void(model), "error": error,
        "ncols": len(columns),
    })
