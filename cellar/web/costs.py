"""Cost period + reconciliation screens (read-only; closing is a command)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from cellar.models import CostEntry, CostPeriod
from cellar.services import cost_ledger


@login_required
def cost_periods(request):
    periods = list(CostPeriod.objects.all()[:36])
    rows = cost_ledger.reconcile()
    return render(request, "web/cost_periods.html", {
        "nav": "reports", "periods": periods,
        "recon": rows, "bad": [r for r in rows if not r["ok"]],
        "wip": cost_ledger.wip_total(),
        "deferred": CostEntry.objects.filter(voided_at__isnull=True)
                    .exclude(deferred_note="").order_by("-occurred_at")[:25],
    })


@login_required
def cost_period_detail(request, pk):
    period = get_object_or_404(CostPeriod, pk=pk)
    return render(request, "web/cost_period_detail.html", {
        "nav": "reports", "period": period,
        "summary": cost_ledger.period_summary(period),
        "entries": (CostEntry.objects.filter(period=period, voided_at__isnull=True)
                    .select_related("lot").order_by("occurred_at", "id")[:500]),
    })
