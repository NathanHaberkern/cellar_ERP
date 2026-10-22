"""Shipping pages: pull orders, review addresses, holds, build pools (Tranche 2),
then per pool: buy labels, print, void, post tracking to C7 (Tranche 4).

Server-rendered pages; every action is a plain POST that redirects back. Each
action checks its own permission, so a Packer sees the pages but not the
compliance override or Void.
"""
from django import forms
from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from integrations.commerce7 import C7Error
from shipping.carriers import CarrierError
from shipping.models import Label, Package, PackPool, ShipOrder
from shipping.permissions import (BUILD_POOLS, OVERRIDE_COMPLIANCE, PRINT_LABELS, SYNC_ORDERS,
                                  VOID_LABELS, perm)
from shipping.services import fulfillment, intake, pools, shipments
from shipping.services.labels import LabelRefused

S = ShipOrder.Status


def _need(request, *codenames):
    if not any(request.user.has_perm(perm(c)) for c in codenames):
        raise PermissionDenied


@login_required
def index(request):
    _need(request, SYNC_ORDERS, BUILD_POOLS, PRINT_LABELS)
    today = timezone.localdate()
    ready, held = pools.eligible_orders(today)
    preview, problems, _ = pools.preview(today)
    open_pools = (PackPool.objects.exclude(status=PackPool.Status.SHIPPED).order_by("pk")
                  .prefetch_related("packages__box", "packages__orders"))
    ctx = {
        "nav": "shipping",
        "gate_on": intake.compliance_gate_on(),
        "av_on": intake.address_validation_enabled(),
        "review": ShipOrder.objects.filter(status=S.ADDRESS_REVIEW).prefetch_related("lines"),
        "new": ShipOrder.objects.filter(status=S.NEW).count(),
        "manual_holds": ShipOrder.objects.filter(status=S.ON_HOLD).prefetch_related("lines"),
        "held": held,
        "ready": ready,
        "preview": preview,
        "problems": problems,
        "open_pools": open_pools,
        "changed": ShipOrder.objects.exclude(c7_change_note="").filter(
            status__in=(S.POOLED, S.LABELED)),
        "can_sync": request.user.has_perm(perm(SYNC_ORDERS)),
        "can_build": request.user.has_perm(perm(BUILD_POOLS)),
        "can_override": request.user.has_perm(perm(OVERRIDE_COMPLIANCE)),
    }
    return render(request, "shipping/index.html", ctx)


@login_required
@require_POST
def pull(request):
    _need(request, SYNC_ORDERS)
    try:
        result = intake.sync_orders()
        messages.success(request, f"Commerce7: {result.summary()}.")
        for err in result.errors[:5]:
            messages.error(request, err)
    except C7Error as e:
        messages.error(request, f"Commerce7: {e}")
        return redirect("shipping")
    _run_checks(request, intake.validate_new_orders)
    return redirect("shipping")


def _run_checks(request, fn):
    try:
        counts = fn()
    except CarrierError as e:
        messages.error(request, f"FedEx address check didn't run: {e}")
        return
    if counts["checked"]:
        messages.success(request, f"FedEx: {counts['ready']} ready, "
                                  f"{counts['review']} need address review.")
    else:
        messages.success(request, f"{counts['ready']} ready. Address check is off "
                                  f"(FedEx test keys) — set Home/Business by hand if needed.")


@login_required
@require_POST
def recheck(request):
    _need(request, SYNC_ORDERS)
    _run_checks(request, intake.recheck_review_queue)
    return redirect("shipping")


@login_required
@require_POST
def order_action(request, pk, action):
    order = get_object_or_404(ShipOrder, pk=pk)
    note = request.POST.get("note", "")
    try:
        if action == "accept":
            _need(request, SYNC_ORDERS)
            intake.accept_suggestion(order, request.user)
        elif action == "keep":
            _need(request, SYNC_ORDERS)
            intake.keep_address(order, request.user)
        elif action == "hold":
            _need(request, SYNC_ORDERS)
            intake.put_on_hold(order, note)
        elif action == "release":
            _need(request, SYNC_ORDERS)
            intake.release_hold(order)
        elif action == "override":
            _need(request, OVERRIDE_COMPLIANCE)
            intake.override_compliance(order, request.user, note)
        elif action == "carrier":
            _need(request, SYNC_ORDERS)
            intake.set_carrier_override(order, request.POST.get("value", ""))
        elif action == "residential":
            _need(request, SYNC_ORDERS)
            value = {"yes": True, "no": False}.get(request.POST.get("value"))
            intake.set_residential(order, value)
        else:
            raise PermissionDenied
        messages.success(request, f"#{order.c7_order_number}: done.")
    except ValueError as e:
        messages.error(request, f"#{order.c7_order_number}: {e}")
    return redirect("shipping")


class AddressForm(forms.Form):
    ship_name = forms.CharField(label="Name", max_length=120)
    ship_company = forms.CharField(label="Company", max_length=120, required=False)
    ship_street1 = forms.CharField(label="Street", max_length=120)
    ship_street2 = forms.CharField(label="Apt / suite", max_length=120, required=False)
    ship_city = forms.CharField(label="City", max_length=80)
    ship_state = forms.CharField(label="State", max_length=2)
    ship_postal_code = forms.CharField(label="ZIP", max_length=10)


@login_required
def edit_address(request, pk):
    _need(request, SYNC_ORDERS)
    order = get_object_or_404(ShipOrder, pk=pk)
    if order.status not in intake.OPEN_EDITABLE:
        messages.error(request, "This order is already in a pool — dissolve the pool to edit it.")
        return redirect("shipping")
    form = AddressForm(request.POST or None, initial=order.address_dict())
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data, ship_state=form.cleaned_data["ship_state"].upper())
        try:
            intake.edit_address(order, request.user, data)
        except CarrierError as e:
            messages.error(request, f"Saved, but FedEx check failed: {e}")
            return redirect("shipping")
        messages.success(request, f"#{order.c7_order_number}: address saved — "
                                  f"{order.get_status_display().lower()}.")
        return redirect("shipping")
    return render(request, "shipping/address_form.html",
                  {"nav": "shipping", "form": form, "order": order})


@login_required
@require_POST
def build(request):
    _need(request, BUILD_POOLS)
    made, problems = pools.build_pools(request.user)
    if made:
        messages.success(request, f"Built {len(made)} pool{'s' if len(made) > 1 else ''}: "
                                  + ", ".join(p.name for p in made) + ".")
    else:
        messages.error(request, "Nothing ready to pool.")
    for number, msg in problems[:5]:
        messages.error(request, f"#{number}: {msg}")
    return redirect("shipping")


@login_required
@require_POST
def dissolve(request, pk):
    _need(request, BUILD_POOLS)
    pool = get_object_or_404(PackPool, pk=pk)
    try:
        n = pools.dissolve_pool(pool)
        messages.success(request, f"Dissolved — {n} orders back to ready.")
    except pools.PoolLocked as e:
        messages.error(request, str(e))
    return redirect("shipping")


# --- Tranche 4: one pool -- labels, printing, C7 ------------------------------

def _carrier_env(carrier):
    return settings.SHIPPING[{"fedex": "FEDEX", "ups": "UPS"}[carrier]]["ENV"]


@login_required
def pool_detail(request, pk):
    _need(request, BUILD_POOLS, PRINT_LABELS)
    pool = get_object_or_404(PackPool, pk=pk)
    packages = (pool.packages.select_related("box")
                .prefetch_related("orders__fulfillment_posts", "labels"))
    rows = []
    for p in packages:
        labels = sorted(p.labels.all(), key=lambda lb: lb.created_at, reverse=True)
        rows.append({"package": p, "orders": list(p.orders.all()),
                     "live": next((lb for lb in labels if lb.voided_at is None), None),
                     "voided": [lb for lb in labels if lb.voided_at is not None]})
    orders = {o.pk: o for r in rows for o in r["orders"]}.values()
    env = _carrier_env(pool.carrier)
    ctx = {
        "nav": "shipping", "pool": pool, "rows": rows,
        "orders": sorted(orders, key=lambda o: o.c7_order_number),
        "env": env, "sandbox": env != "production",
        "writeback_on": fulfillment.writeback_on(),
        "missing": sum(1 for r in rows if r["live"] is None),
        "labeled": sum(1 for r in rows if r["live"] is not None),
        "changed": [o for o in orders if o.c7_change_note],
        "can_print": request.user.has_perm(perm(PRINT_LABELS)),
        "can_void": request.user.has_perm(perm(VOID_LABELS)),
        "can_build": request.user.has_perm(perm(BUILD_POOLS)),
    }
    return render(request, "shipping/pool.html", ctx)


@login_required
@require_POST
def pool_buy(request, pk):
    _need(request, PRINT_LABELS)
    pool = get_object_or_404(PackPool, pk=pk)
    try:
        bought, errors = shipments.buy_pool_labels(pool, operator=request.user)
    except (CarrierError, LabelRefused) as e:
        messages.error(request, str(e))
        return redirect("shipping-pool", pk=pk)
    if bought:
        messages.success(request, f"Bought {len(bought)} label{'s' if len(bought) > 1 else ''}.")
    for package, err in errors[:8]:
        messages.error(request, f"Box {package.sequence}: {err}")
    if not bought and not errors:
        messages.success(request, "Every box already has a label.")
    return redirect("shipping-pool", pk=pk)


@login_required
@require_POST
def package_buy(request, pk):
    _need(request, PRINT_LABELS)
    package = get_object_or_404(Package, pk=pk)
    try:
        label = shipments.buy_package_label(package, operator=request.user)
        messages.success(request, f"Box {package.sequence}: {label.tracking_number}.")
    except (CarrierError, LabelRefused) as e:
        messages.error(request, f"Box {package.sequence}: {e}")
    return redirect("shipping-pool", pk=package.pool_id)


@login_required
@require_POST
def label_void(request, pk):
    _need(request, VOID_LABELS)
    label = get_object_or_404(Label, pk=pk)
    try:
        shipments.void_package_label(label, operator=request.user)
        messages.success(request, f"Voided {label.tracking_number}.")
    except (CarrierError, LabelRefused) as e:
        messages.error(request, f"{label.tracking_number}: {e}")
    if label.package_id:
        return redirect("shipping-pool", pk=label.package.pool_id)
    return redirect("shipping")


def _pdf(data, filename):
    resp = HttpResponse(data, content_type="application/pdf")
    # inline: opens in the browser's viewer, which prints 4x6 straight to the thermal printer
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


@login_required
def label_pdf(request, pk):
    _need(request, PRINT_LABELS)
    label = get_object_or_404(Label.objects.prefetch_related("documents"), pk=pk)
    try:
        return _pdf(shipments.merged_pdf([label]), f"{label.tracking_number}.pdf")
    except LabelRefused as e:
        messages.error(request, str(e))
        return redirect("shipping")


@login_required
def pool_pdf(request, pk):
    _need(request, PRINT_LABELS)
    pool = get_object_or_404(PackPool, pk=pk)
    try:
        data = shipments.merged_pdf(shipments.pool_live_labels(pool))
    except LabelRefused as e:
        messages.error(request, str(e))
        return redirect("shipping-pool", pk=pk)
    return _pdf(data, f"pool-{pool.pk}-labels.pdf")


def _report_posts(request, result):
    level = messages.error if result.errors else messages.success
    level(request, f"Commerce7: {result.summary()}.")
    for number, msg in result.errors[:8]:
        messages.error(request, f"#{number}: {msg}")
    for number, msg in result.skipped[:8]:
        messages.info(request, f"#{number}: {msg}")


@login_required
@require_POST
def pool_post(request, pk):
    _need(request, PRINT_LABELS)
    pool = get_object_or_404(PackPool, pk=pk)
    try:
        result = fulfillment.post_pool(pool, operator=request.user)
    except C7Error as e:
        messages.error(request, f"Commerce7: {e}")
        return redirect("shipping-pool", pk=pk)
    _report_posts(request, result)
    return redirect("shipping-pool", pk=pk)


@login_required
@require_POST
def order_post(request, pk):
    _need(request, PRINT_LABELS)
    order = get_object_or_404(ShipOrder, pk=pk)
    try:
        outcome, msg = fulfillment.post_order(order, operator=request.user)
    except C7Error as e:
        outcome, msg = "error", str(e)
    (messages.error if outcome == "error" else messages.success)(
        request, f"#{order.c7_order_number}: {outcome} — {msg}")
    pool = PackPool.objects.filter(packages__orders=order).order_by("-pk").first()
    if pool:
        shipments.refresh_pool(pool)
        return redirect("shipping-pool", pk=pool.pk)
    return redirect("shipping")
