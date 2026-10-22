"""Shipping page (Tranche 2): pull orders, review addresses, holds, build pools.

One server-rendered page plus an address-edit form. Every action is a plain
POST that redirects back (no HTMX yet -- the packing/print pages in Tranche 4
are where interactivity pays off). Each action checks its own permission, so a
Packer sees the page but not the compliance override.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from integrations.commerce7 import C7Error
from shipping.carriers import CarrierError
from shipping.models import PackPool, ShipOrder
from shipping.permissions import (BUILD_POOLS, OVERRIDE_COMPLIANCE, SYNC_ORDERS, perm)
from shipping.services import intake, pools

S = ShipOrder.Status


def _need(request, *codenames):
    if not any(request.user.has_perm(perm(c)) for c in codenames):
        raise PermissionDenied


@login_required
def index(request):
    _need(request, SYNC_ORDERS, BUILD_POOLS)
    today = timezone.localdate()
    ready, held = pools.eligible_orders(today)
    preview, problems, _ = pools.preview(today)
    open_pools = (PackPool.objects.filter(status=PackPool.Status.OPEN).order_by("pk")
                  .prefetch_related("packages__box", "packages__orders"))
    ctx = {
        "nav": "shipping",
        "gate_on": intake.compliance_gate_on(),
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
    try:
        counts = intake.validate_new_orders()
        messages.success(request, f"FedEx: {counts['ready']} ready, "
                                  f"{counts['review']} need address review.")
    except CarrierError as e:
        messages.error(request, f"FedEx address check didn't run: {e}")
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
