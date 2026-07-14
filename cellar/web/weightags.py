"""
Weigh tag list + detail — read-only views.

There was previously no in-app way to look at a WeighTag once it had been
entered at intake (only Django admin). This gives the GM a list of tags with
remaining/allocated pounds at a glance, and a detail page per tag showing its
bin lines and which lot(s) it fed.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from cellar.models import WeighTag


@login_required
def weightag_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = (WeighTag.objects.select_related("harvest_event__block__vineyard__grower")
          .order_by("-created_at"))
    if q:
        qs = qs.filter(weigh_tag_number__icontains=q)

    rows = []
    for wt in qs[:300]:
        block = wt.harvest_event.block if wt.harvest_event_id else None
        rows.append({
            "wt": wt,
            "vineyard": block.vineyard.name if block else "",
            "block": block.name if block else "",
            "net_total": wt.net_total,
            "allocated_lbs": wt.allocated_lbs,
            "remaining_lbs": wt.remaining_lbs,
        })

    return render(request, "web/weightag_list.html", {
        "nav": "weightags", "rows": rows, "q": q,
    })


@login_required
def weightag_detail(request, pk):
    wt = get_object_or_404(
        WeighTag.objects.select_related("harvest_event__block__vineyard__grower"), pk=pk)
    bins = wt.bins.select_related("assigned_lot__current_designation").order_by("id")
    allocations = (wt.allocations.filter(voided_at__isnull=True)
                   .select_related("lot__current_designation").order_by("id"))
    # Lots fed by this tag either via a direct (net-only) allocation, or via a
    # bin line assigned to a lot — a tag can feed several lots either way.
    lots = {a.lot for a in allocations} | {b.assigned_lot for b in bins if b.assigned_lot_id}

    return render(request, "web/weightag_detail.html", {
        "nav": "weightags", "wt": wt, "bins": bins,
        "allocations": allocations,
        "lots": sorted(lots, key=lambda l: l.pk, reverse=True),
    })
