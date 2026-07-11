"""
Tasks — front-end (HTMX).

The dashboard shows every open task (filterable by assignee); each lot page shows
its own tasks and a quick-add form. Actions (complete / reopen / delete / reassign)
are shared: a `scope` field on the POST says which fragment to re-render —
'lot' (with a lot pk) swaps the lot Tasks panel, otherwise the dashboard list.

The Rules menu lists the auto-task rules and lets Nate tune each rule's parameters
and toggle it on/off.
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import Lot, Task, TaskRule
from cellar.services import tasks as tsvc

User = get_user_model()


def _users():
    return User.objects.order_by("username")


def _resolve_assignee(raw):
    """'' → leave unset (None handled by caller); 'none' → unassigned;
    a pk → that user."""
    raw = (raw or "").strip()
    if raw == "none" or raw == "":
        return None
    return User.objects.filter(pk=raw).first()


# --------------------------------------------------------- dashboard list --
def dash_tasks_ctx(request):
    """Context for the dashboard task list, honouring an assignee filter that may
    arrive as a GET query (filter change) or a POST field (after an action)."""
    f = (request.POST.get("assignee_filter") or request.GET.get("assignee") or "all").strip()
    qs = tsvc.open_tasks()
    if f == "none":
        qs = qs.filter(assignee__isnull=True)
    elif f not in ("all", ""):
        qs = qs.filter(assignee_id=f)
    return {"tasks": qs, "users": _users(), "assignee_filter": f}


@login_required
def dash_tasks(request):
    return render(request, "web/_dash_tasks.html", dash_tasks_ctx(request))


# --------------------------------------------------------------- lot panel --
def lot_tasks_ctx(lot):
    return {"lot": lot, "tasks": tsvc.open_tasks(lot=lot), "users": _users()}


@login_required
@require_http_methods(["POST"])
def lot_task_create(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    title = (request.POST.get("title") or "").strip()
    if title:
        tsvc.create_task(
            title=title,
            body=(request.POST.get("body") or "").strip(),
            due_date=parse_date(request.POST.get("due_date") or ""),
            assignee=_resolve_assignee(request.POST.get("assignee")),
            lot=lot, actor=request.user)
    return render(request, "web/_lot_tasks.html", lot_tasks_ctx(lot))


# ------------------------------------------------------------- shared acts --
def _scope_response(request):
    """Re-render whichever fragment the action came from."""
    if request.POST.get("scope") == "lot":
        lot = get_object_or_404(Lot, pk=request.POST.get("lot"))
        return render(request, "web/_lot_tasks.html", lot_tasks_ctx(lot))
    return render(request, "web/_dash_tasks.html", dash_tasks_ctx(request))


@login_required
@require_http_methods(["POST"])
def task_action(request, pk):
    task = get_object_or_404(Task, pk=pk)
    action = request.POST.get("action")
    if action == "complete":
        tsvc.complete_task(task, actor=request.user)
    elif action == "reopen":
        tsvc.reopen_task(task, actor=request.user)
    elif action == "delete":
        tsvc.delete_task(task, actor=request.user)
    return _scope_response(request)


@login_required
@require_http_methods(["POST"])
def task_reassign(request, pk):
    task = get_object_or_404(Task, pk=pk)
    tsvc.reassign_task(task, _resolve_assignee(request.POST.get("assignee")),
                       actor=request.user)
    return _scope_response(request)


# ------------------------------------------------------------- rules menu --
@login_required
def rules_index(request):
    return render(request, "web/rules.html",
                  {"nav": "rules", "rules": TaskRule.objects.order_by("name"),
                   "status_choices": Lot.Status.choices})


@login_required
@require_http_methods(["POST"])
def rule_update(request, pk):
    rule = get_object_or_404(TaskRule, pk=pk)
    rule.enabled = request.POST.get("enabled") == "on"

    # friendly params by rule; fall back to raw JSON for anything else
    if rule.key == "topping_interval":
        try:
            rule.params = {"interval_days": int(request.POST.get("interval_days") or 60)}
        except ValueError:
            pass
    elif rule.key == "ferment_daily":
        statuses = request.POST.getlist("statuses") or ["fermenting"]
        rule.params = {"statuses": statuses}
    else:
        raw = request.POST.get("params_json")
        if raw:
            try:
                rule.params = json.loads(raw)
            except json.JSONDecodeError:
                pass

    rule.save(update_fields=["enabled", "params"])
    return render(request, "web/_rule_row.html",
                  {"rule": rule, "saved": True,
                   "status_choices": Lot.Status.choices})
