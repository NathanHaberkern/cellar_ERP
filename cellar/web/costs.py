"""Cost period + reconciliation screens (read-only; closing is a command)."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

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


@login_required
@require_http_methods(["GET", "POST"])
def overhead_pools(request):
    """Enter monthly pool dollars, and preview/see what allocation did."""
    from decimal import Decimal, InvalidOperation

    from cellar.models import OverheadPool, OverheadPoolPeriod
    from cellar.services import overhead

    overhead.ensure_default_pools()

    period_id = request.POST.get("period") or request.GET.get("period")
    period = (CostPeriod.objects.filter(pk=period_id).first() if period_id
              else CostPeriod.objects.filter(status=CostPeriod.Status.OPEN)
              .order_by("-year", "-month").first())

    error = None
    if request.method == "POST" and period is not None:
        if not period.is_open:
            error = f"{period.label} is {period.get_status_display().lower()} — amounts are frozen."
        else:
            for pool in OverheadPool.objects.filter(active=True):
                raw = (request.POST.get(f"amount:{pool.pk}") or "").strip()
                if raw == "":
                    continue
                try:
                    amount = Decimal(raw)
                except InvalidOperation:
                    error = f"{pool.name}: '{raw}' isn't a number."
                    break
                existing = OverheadPoolPeriod.objects.filter(
                    pool=pool, period=period, voided_at__isnull=True).first()
                if existing:
                    if existing.is_allocated or existing.amount == amount:
                        continue
                    existing.voided_at = timezone.now()
                    existing.save(update_fields=["voided_at"])
                OverheadPoolPeriod.objects.create(
                    pool=pool, period=period, amount=amount, operator=request.user)

    plan = overhead.preview(period) if period else None
    entered = {}
    if period:
        for pp in OverheadPoolPeriod.objects.filter(period=period, voided_at__isnull=True):
            entered[pp.pool_id] = pp

    return render(request, "web/overhead_pools.html", {
        "nav": "reports", "period": period, "plan": plan, "error": error,
        "periods": CostPeriod.objects.all()[:24],
        "pools": OverheadPool.objects.filter(active=True),
        "entered": entered,
        "normal_capacity": overhead.normal_capacity_gal(),
        "max_years": overhead.absorption_max_years(),
    })
