"""Club desk page: every member request, what we propose, and Approve / Apply.

Approve and Apply are separate clicks on purpose: approving records who said yes;
applying is the moment Commerce7 is written to. Both need approve_club_changes.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clubdesk.models import ClubRequest
from clubdesk.permissions import APPROVE_CHANGES, VIEW_REQUESTS, perm
from clubdesk.services import apply as applier
from clubdesk.services.propose import propose
from integrations.commerce7 import C7Error, Commerce7Client

S = ClubRequest.Status


def _need(request, *codenames):
    if not any(request.user.has_perm(perm(c)) for c in codenames):
        raise PermissionDenied


@login_required
def index(request):
    _need(request, VIEW_REQUESTS, APPROVE_CHANGES)
    open_ = ClubRequest.objects.exclude(status__in=(S.APPLIED, S.DISMISSED)).order_by("received_at")
    done = ClubRequest.objects.filter(status=S.APPLIED).order_by("-applied_at")[:30]
    return render(request, "clubdesk/index.html", {
        "nav": "clubdesk", "open": open_, "done": done,
        "can_approve": request.user.has_perm(perm(APPROVE_CHANGES)),
    })


@login_required
@require_POST
def action(request, pk, verb):
    _need(request, APPROVE_CHANGES)
    req = get_object_or_404(ClubRequest, pk=pk)
    if verb == "approve":
        if req.status != S.PROPOSED:
            messages.error(request, "Only a proposed, unblocked request can be approved.")
        else:
            req.status, req.approved_by, req.approved_at = S.APPROVED, request.user, timezone.now()
            req.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    elif verb == "apply":
        try:
            ok = applier.apply(req, request.user, Commerce7Client.from_settings())
        except applier.NotApproved as e:
            messages.error(request, str(e))
        else:
            (messages.success if ok else messages.error)(
                request, f"{req}: " + ("applied, note added to the C7 account." if ok else req.error))
    elif verb == "replan":
        try:
            propose(req, Commerce7Client.from_settings())
        except C7Error as e:
            messages.error(request, str(e))
    elif verb == "dismiss":
        req.status = S.DISMISSED
        req.save(update_fields=["status", "updated_at"])
    else:
        raise PermissionDenied
    return redirect("clubdesk")
