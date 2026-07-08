"""
Server-rendered HTMX front end for the Cellar ERP.

Pattern: these views render HTML (full pages, or fragments for HTMX swaps) and
call cellar/services/ DIRECTLY -- they do NOT go over the DRF JSON API. The JSON
API (cellar/api/) is for the future iOS client; the browser talks to these views
using the same session auth already configured. One services layer, two consumers.

HTMX usage here:
  - live lot search        -> GET fragment swapped into the results tbody
  - lot detail sub-panels  -> composition / oak / cost lazy-loaded as fragments
  - report rendering       -> POST period, swap the rendered report in place
  - additive CRUD          -> inline add / edit / void without full page reloads

Service calls are bound to the real cellar/services signatures and wrapped so a
data gap shows an inline message rather than 500-ing the page.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cellar.models.spine import Lot
from cellar.models.reference import Additive

from cellar.services import aging as aging_svc
from cellar.services import costing as costing_svc
from cellar.services import reporting as reporting_svc
from cellar.services import excise as excise_svc
from cellar.services import crush_report as crush_svc


def _htmx(request):
    return request.headers.get("HX-Request") == "true"


def _safe(fn, *args, **kwargs):
    """Run a service call; return (result, error_message). Keeps a signature
    mismatch or data gap from 500-ing the page -- the template shows the message."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - deliberately broad at the UI seam
        return None, f"{fn.__module__}.{fn.__name__}: {e}"


# ---------------------------------------------------------------- dashboard --
@login_required
def dashboard(request):
    active_lots = Lot.objects.count()
    ctx = {
        "nav": "dashboard",
        "active_lots": active_lots,
        "today": timezone.localdate(),
    }
    return render(request, "web/dashboard.html", ctx)


# --------------------------------------------------------------------- lots --
@login_required
def lots_list(request):
    ctx = {"nav": "lots", "lots": _lot_queryset(request), "q": request.GET.get("q", "")}
    return render(request, "web/lots_list.html", ctx)


@login_required
def lots_search(request):
    """HTMX fragment: just the table body, swapped as the user types."""
    return render(request, "web/_lots_rows.html", {"lots": _lot_queryset(request)})


def _lot_queryset(request):
    # Lot.code is a derived property (from current_designation), not a column, so
    # search filters in Python over the rendered code. Fine at this scale.
    qs = Lot.objects.select_related("current_designation").order_by("-pk")
    q = (request.GET.get("q") or "").strip().lower()
    if q:
        return [lot for lot in qs if q in (lot.code or "").lower()][:200]
    return list(qs[:200])


@login_required
def lot_detail(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    return render(request, "web/lot_detail.html", {"nav": "lots", "lot": lot})


@login_required
def lot_composition(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    data, err = _safe(aging_svc.composition_of, lot)
    return render(request, "web/_panel.html",
                  {"title": "Composition (leaf lots)", "value": data, "error": err})


@login_required
def lot_oak(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    data, err = _safe(aging_svc.oak_detail, lot)
    return render(request, "web/_panel.html",
                  {"title": "Oak detail", "value": data, "error": err})


@login_required
def lot_cost(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    cost, err1 = _safe(costing_svc.lot_cost, lot)        
    per_gal, err2 = _safe(costing_svc.lot_cost_per_gal, lot)
    value = {"lot_cost": cost, "lot_cost_per_gal": per_gal}
    return render(request, "web/_panel.html",
                  {"title": "Cost", "value": value, "error": err1 or err2})


# ------------------------------------------------------------------ reports --
# key -> (label, needs)  where needs drives which inputs are required
REPORTS = {
    "5120-17":    ("TTB 5120.17 — Part I", "year_month"),
    "5120-17-p3": ("TTB 5120.17 — Part III (spirits)", "year_month"),
    "5120-17-p4": ("TTB 5120.17 — Part IV (materials)", "year_month"),
    "excise":     ("CBMA excise (5000.24 wine line)", "year_dates"),
    "crush":      ("CA crush report", "year"),
}


@login_required
def reports_index(request):
    return render(request, "web/reports.html",
                  {"nav": "reports",
                   "reports": [(k, v[0], v[1]) for k, v in REPORTS.items()]})


def _int(request, key):
    raw = (request.POST.get(key) or "").strip()
    return int(raw) if raw else None


@login_required
@require_http_methods(["POST"])
def report_run(request):
    """HTMX fragment: run the chosen report for the given period, swap result in."""
    from datetime import date as _date
    key = request.POST.get("report")
    entry = REPORTS.get(key)
    if entry is None:
        return render(request, "web/_report_result.html",
                      {"error": "Unknown report.", "title": "Report"})
    title, needs = entry

    try:
        year = _int(request, "year")
        if year is None:
            raise ValueError("Year is required.")
        totals = None
        download = None
        if needs == "year_month":
            month = _int(request, "month")
            if month is None:
                raise ValueError("Month is required for this report.")
            fn = {"5120-17": reporting_svc.build_5120_17,
                  "5120-17-p3": reporting_svc.build_5120_17_part3,
                  "5120-17-p4": reporting_svc.build_5120_17_part4}[key]
            value = fn(year, month)
            if key == "5120-17":
                download = f"/api/reports/5120-17/pdf/?year={year}&month={month}"
        elif needs == "year_dates":
            start = request.POST.get("start") or ""
            end = request.POST.get("end") or ""
            if not start or not end:
                raise ValueError("Start and end dates are required (YYYY-MM-DD).")
            value = excise_svc.compute_period_excise(
                year, _date.fromisoformat(start), _date.fromisoformat(end))
        else:  # year only -> crush
            rows = crush_svc.ca_crush_report(year)
            totals = crush_svc.crush_report_totals(rows)
            value = rows
            download = f"/api/reports/crush/pdf/?year={year}"
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_report_result.html", {"error": str(e), "title": title})

    return render(request, "web/_report_result.html",
                  {"title": title, "value": value, "totals": totals,
                   "download": download, "report_key": key,
                   "period": {"year": year}})


# ------------------------------------------------ reference CRUD (additives) --
# Reference pattern to replicate for other masters. Additives are editable (not
# append-only), so straight create/update. `unit_cost` is a documented field.
@login_required
def additives(request):
    return render(request, "web/additives.html",
                  {"nav": "reference",
                   "additives": Additive.objects.order_by("name"),
                   "categories": Additive.Category.choices})


@login_required
@require_http_methods(["POST"])
def additive_create(request):
    name = (request.POST.get("name") or "").strip()
    category = (request.POST.get("category") or "").strip()
    unit = (request.POST.get("unit") or "").strip()
    valid_cats = {c for c, _ in Additive.Category.choices}
    if not name or category not in valid_cats or not unit:
        return render(request, "web/_additive_row.html",
                      {"error": "Name, a valid category, and unit are all required.",
                       "additive": None}, status=400)
    obj = Additive(name=name, category=category, unit=unit)
    _apply_unit_cost(obj, request.POST.get("unit_cost"))
    obj.save()
    return render(request, "web/_additive_row.html", {"additive": obj})


@login_required
@require_http_methods(["POST"])
def additive_update(request, pk):
    obj = get_object_or_404(Additive, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if name:
        obj.name = name
    category = (request.POST.get("category") or "").strip()
    if category in {c for c, _ in Additive.Category.choices}:
        obj.category = category
    unit = (request.POST.get("unit") or "").strip()
    if unit:
        obj.unit = unit
    _apply_unit_cost(obj, request.POST.get("unit_cost"))
    obj.save()
    return render(request, "web/_additive_row.html", {"additive": obj})


def _apply_unit_cost(obj, raw):
    raw = (raw or "").strip()
    if raw == "":
        return
    try:
        obj.unit_cost = raw  # DecimalField accepts the string
    except Exception:
        pass
