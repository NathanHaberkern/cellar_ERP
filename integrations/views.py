"""QuickBooks connect page: one-time consent, then the app refreshes on its own.

Superuser only -- connecting picks which QuickBooks company the ERP reads and
writes. The callback URL must be listed as a Redirect URI on the Intuit app
(settings.QBO["REDIRECT_URI"]).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from integrations import qbo


def _superuser(request):
    if not request.user.is_superuser:
        raise PermissionDenied


@login_required
def qbo_status(request):
    _superuser(request)
    try:
        qbo._conf()
        configured = True
    except qbo.QBOConfigError as e:
        configured = False
        messages.error(request, str(e))
    return render(request, "integrations/qbo.html",
                  {"nav": "", "conn": qbo.connection(), "configured": configured})


@login_required
@require_POST
def qbo_connect(request):
    _superuser(request)
    try:
        url, state = qbo.authorize_url()
    except qbo.QBOConfigError as e:
        messages.error(request, str(e))
        return redirect("qbo-status")
    request.session["qbo_oauth_state"] = state
    return redirect(url)


@login_required
def qbo_callback(request):
    _superuser(request)
    expected = request.session.pop("qbo_oauth_state", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "QuickBooks sign-in didn't match this session -- try Connect again.")
        return redirect("qbo-status")
    if request.GET.get("error"):
        messages.error(request, f"QuickBooks: {request.GET['error']}")
        return redirect("qbo-status")
    code, realm = request.GET.get("code"), request.GET.get("realmId")
    if not code or not realm:
        messages.error(request, "QuickBooks didn't send back a company -- try Connect again.")
        return redirect("qbo-status")
    try:
        conn = qbo.complete_connect(code, realm)
    except qbo.QBOError as e:
        messages.error(request, str(e))
        return redirect("qbo-status")
    messages.success(request, f"Connected to {conn.company_name or 'QuickBooks'} ({conn.realm_id}).")
    return redirect("qbo-status")


@login_required
@require_POST
def qbo_disconnect(request):
    _superuser(request)
    try:
        revoked = qbo.disconnect()
    except qbo.QBOConfigError as e:
        messages.error(request, str(e))
        return redirect("qbo-status")
    if revoked:
        messages.success(request, "Disconnected from QuickBooks and revoked the app's access.")
    else:
        messages.error(request, "Disconnected here, but Intuit didn't confirm the revoke -- "
                                "also remove the app under QuickBooks > Apps > My apps.")
    return redirect("qbo-status")
