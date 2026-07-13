"""
Daily Checklist + Daily Plan — front end (HTMX).

One page (`/daily/`), two sections sharing a date:
  * Checklist — today's fermentation housekeeping across every fermenting lot;
    the quick-log form calls the exact same service the lot-page Fermentation
    tab uses (fermentation.record_daily), so a log here and a log on the lot
    page are indistinguishable in the ledger.
  * Plan — the editable DailyPlan for the date (see services/daily_plan.py):
    auto-drafted once, then toggled/added/removed/regenerated in place.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Lot, DailyPlan
from cellar.services import daily_plan as plan_svc
from cellar.services import fermentation as ferm_svc


def _resolve_date(request):
    d = parse_date(request.GET.get("date") or request.POST.get("date") or "")
    return d or timezone.localdate()


def _checklist_ctx(today):
    return {"today": today, "checklist": plan_svc.checklist_rows(today)}


def _plan_ctx(plan):
    return {"plan": plan, "groups": plan_svc.grouped_items(plan),
            "categories": plan_svc.CATEGORIES}


@login_required
def daily_index(request):
    today = _resolve_date(request)
    plan = plan_svc.get_or_create_plan(today)
    ctx = {"nav": "daily", "today": today,
           "prev_date": (today - timedelta(days=1)).isoformat(),
           "next_date": (today + timedelta(days=1)).isoformat(),
           "is_today": today == timezone.localdate()}
    ctx.update(_checklist_ctx(today))
    ctx.update(_plan_ctx(plan))
    return render(request, "web/daily.html", ctx)


@login_required
@require_http_methods(["POST"])
def daily_quick_log(request, lot_pk):
    """Checklist row's inline log form — same service call the lot page's
    Fermentation tab uses, just rendered back into the checklist fragment
    instead of the lot panel."""
    lot = get_object_or_404(Lot, pk=lot_pk)
    today = _resolve_date(request)
    error = None
    try:
        ferm_svc.record_daily(
            lot, brix=request.POST.get("brix"), temp=request.POST.get("temp"),
            cap=request.POST.get("cap") or None, actor=request.user)
    except Exception as e:  # noqa: BLE001
        error = str(e)
    ctx = _checklist_ctx(today)
    ctx["error"] = error
    return render(request, "web/_daily_checklist.html", ctx)


@login_required
@require_http_methods(["POST"])
def daily_item_toggle(request, plan_pk, item_id):
    plan = get_object_or_404(DailyPlan, pk=plan_pk)
    plan_svc.toggle_item(plan, item_id)
    return render(request, "web/_daily_plan.html", _plan_ctx(plan))


@login_required
@require_http_methods(["POST"])
def daily_item_add(request, plan_pk):
    plan = get_object_or_404(DailyPlan, pk=plan_pk)
    category = request.POST.get("category") or "other"
    plan_svc.add_manual_item(plan, category, request.POST.get("label"),
                             request.POST.get("detail"))
    return render(request, "web/_daily_plan.html", _plan_ctx(plan))


@login_required
@require_http_methods(["POST"])
def daily_item_remove(request, plan_pk, item_id):
    plan = get_object_or_404(DailyPlan, pk=plan_pk)
    plan_svc.remove_item(plan, item_id)
    return render(request, "web/_daily_plan.html", _plan_ctx(plan))


@login_required
@require_http_methods(["POST"])
def daily_regenerate(request, plan_pk):
    plan = get_object_or_404(DailyPlan, pk=plan_pk)
    added = plan_svc.add_suggestions(plan)
    ctx = _plan_ctx(plan)
    ctx["added"] = added
    return render(request, "web/_daily_plan.html", ctx)
