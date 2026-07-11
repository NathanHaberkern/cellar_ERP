"""
Lab CSV import — front-end upload of an ETS report.

Flow (HTMX):
  GET  /labs/import/          → upload page
  POST /labs/import/preview/  → parse the file, show a preview (no writes)
  POST /labs/import/commit/   → commit the same CSV text (idempotent)

Preview and commit both work off the raw CSV *text*: the preview embeds it in a
hidden field so commit re-parses the identical bytes — no server-side session
state, and the dedupe key makes a double-submit harmless anyway.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from cellar.models import Lot
from cellar.services import labimport


def _lots():
    """Lot choices for binding an unresolved sample description, newest first."""
    return sorted(Lot.objects.select_related("current_designation"),
                  key=lambda l: (-l.vintage_year, l.code))


def _binds(request):
    """{description: lot_pk} from the preview's bind_<description> selects."""
    return {k[len("bind_"):]: v
            for k, v in request.POST.items()
            if k.startswith("bind_") and (v or "").strip()}


@login_required
def labs_import_index(request):
    return render(request, "web/labs_import.html", {"nav": "labs"})


def _read_upload(request):
    f = request.FILES.get("csv")
    if f is not None:
        return f.read().decode("utf-8-sig", errors="replace")
    # commit posts the text back in a hidden field
    return request.POST.get("csv_text", "")


@login_required
@require_http_methods(["POST"])
def labs_import_preview(request):
    text = _read_upload(request)
    if not text.strip():
        return render(request, "web/_labs_import_preview.html",
                      {"plan": None, "lots": _lots(),
                       "error": "No file received. Choose an ETS .csv and try again."})
    plan = labimport.plan(text)
    return render(request, "web/_labs_import_preview.html",
                  {"plan": plan, "error": plan.error, "csv_text": text, "lots": _lots()})


@login_required
@require_http_methods(["POST"])
def labs_import_commit(request):
    text = request.POST.get("csv_text", "")
    try:
        results, values = labimport.commit(text, user=request.user, binds=_binds(request))
        # re-plan so the confirmation reflects post-commit state (everything now dup)
        plan = labimport.plan(text)
        return render(request, "web/_labs_import_preview.html",
                      {"plan": plan, "committed": True,
                       "committed_results": results, "committed_values": values,
                       "csv_text": text, "lots": _lots()})
    except Exception as e:  # noqa: BLE001
        return render(request, "web/_labs_import_preview.html",
                      {"plan": labimport.plan(text), "error": str(e), "csv_text": text,
                       "lots": _lots()})
