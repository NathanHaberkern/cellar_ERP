"""
Inventory screens: on-hand, receive, physical count, write-down.

All keyed off invoices — no scanning (Nate's call; ~40 additives and ~60 dry goods
is a filterable list, not the 1,100-barrel scan-first problem).
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from cellar.models import (
    Additive, DryGood, Material, PhysicalCount, StockTransaction,
)
from cellar.services import stock as stock_svc

CATALOGS = {"additive": Additive, "dry_good": DryGood, "material": Material}
KIND_LABELS = [("additive", "Additives"), ("dry_good", "Dry goods"), ("material", "Materials")]


def _dec(raw, default=None):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return default


def _resolve(item_key):
    """'additive:12' -> the Additive instance. Returns None on anything malformed."""
    try:
        kind, pk = (item_key or "").split(":", 1)
        model = CATALOGS[kind]
    except (ValueError, KeyError):
        return None
    return model.objects.filter(pk=pk).first()


def _item_choices():
    """[(value, label, unit)] across all three catalogs, for the pickers."""
    out = []
    for key, model in CATALOGS.items():
        for obj in model.objects.order_by("name"):
            out.append({"value": f"{key}:{obj.pk}", "label": obj.name,
                        "unit": obj.unit,
                        "group": dict(KIND_LABELS)[key]})
    return out


# ------------------------------------------------------------------- on hand
@login_required
def stock_index(request):
    kind = request.GET.get("kind") or ""
    q = (request.GET.get("q") or "").strip().lower()
    kinds = (kind,) if kind in CATALOGS else tuple(CATALOGS)

    rows = stock_svc.stock_report(kinds, include_zero=request.GET.get("all") == "1")
    if q:
        rows = [r for r in rows if q in r["item"].name.lower()]

    return render(request, "web/stock_index.html", {
        "nav": "inventory", "rows": rows,
        "total_value": sum((r["value"] for r in rows), Decimal("0")),
        "negatives": [r for r in rows if r["negative"]],
        "kind": kind, "q": request.GET.get("q") or "",
        "show_all": request.GET.get("all") == "1",
        "kind_labels": KIND_LABELS,
    })


@login_required
def stock_item(request, kind, pk):
    model = CATALOGS.get(kind)
    if model is None:
        return redirect("stock-index")
    item = get_object_or_404(model, pk=pk)
    txns = (StockTransaction.objects.filter(voided_at__isnull=True,
                                            **stock_svc.item_filter(item))
            .order_by("-occurred_at", "-id"))
    return render(request, "web/stock_item.html", {
        "nav": "inventory", "item": item, "kind": kind, "txns": txns,
        "on_hand": stock_svc.on_hand(item),
        "value": stock_svc.on_hand_value(item),
        "wac": stock_svc.wac(item),
    })


# ------------------------------------------------------------------- receive
@login_required
@require_http_methods(["GET", "POST"])
def stock_receive(request):
    ctx = {"nav": "inventory", "items": _item_choices(),
           "today": timezone.localdate().isoformat()}

    if request.method == "POST":
        item = _resolve(request.POST.get("item"))
        if item is None:
            ctx["error"] = "Pick an item to receive."
            return render(request, "web/stock_receive.html", ctx, status=400)

        goods = _dec(request.POST.get("goods_cost"))
        if goods is None or goods < 0:
            ctx["error"] = "Enter the goods cost from the invoice."
            return render(request, "web/stock_receive.html", ctx, status=400)

        try:
            txn = stock_svc.receive(
                item,
                pack_count=_dec(request.POST.get("pack_count"), Decimal("0")),
                pack_size=_dec(request.POST.get("pack_size"), Decimal("0")),
                pack_unit=(request.POST.get("pack_unit") or "").strip(),
                goods_cost=goods,
                freight_cost=_dec(request.POST.get("freight_cost"), Decimal("0")),
                tax_cost=_dec(request.POST.get("tax_cost"), Decimal("0")),
                occurred_at=parse_date(request.POST.get("occurred_at") or "")
                            or timezone.localdate(),
                supplier=(request.POST.get("supplier") or "").strip(),
                reference=(request.POST.get("reference") or "").strip(),
                operator=request.user,
            )
        except (ValueError, stock_svc.UnitMismatch) as e:
            ctx["error"] = str(e)
            return render(request, "web/stock_receive.html", ctx, status=400)

        ctx["received"] = txn
        ctx["new_wac"] = stock_svc.wac(item)
        ctx["new_on_hand"] = stock_svc.on_hand(item)
    return render(request, "web/stock_receive.html", ctx)


# ---------------------------------------------------------------- write-down
@login_required
@require_http_methods(["GET", "POST"])
def stock_write_down(request):
    ctx = {"nav": "inventory", "items": _item_choices(),
           "today": timezone.localdate().isoformat()}

    if request.method == "POST":
        item = _resolve(request.POST.get("item"))
        if item is None:
            ctx["error"] = "Pick an item."
            return render(request, "web/stock_write_down.html", ctx, status=400)
        try:
            txn = stock_svc.write_down(
                item, _dec(request.POST.get("quantity"), Decimal("0")),
                reason=(request.POST.get("reason") or "").strip(),
                occurred_at=parse_date(request.POST.get("occurred_at") or "")
                            or timezone.localdate(),
                operator=request.user,
            )
        except ValueError as e:
            ctx["error"] = str(e)
            return render(request, "web/stock_write_down.html", ctx, status=400)
        ctx["written"] = txn
        ctx["new_on_hand"] = stock_svc.on_hand(item)
    return render(request, "web/stock_write_down.html", ctx)


# --------------------------------------------------------------------- count
@login_required
def count_list(request):
    return render(request, "web/stock_counts.html", {
        "nav": "inventory",
        "counts": PhysicalCount.objects.filter(voided_at__isnull=True)[:50],
        "today": timezone.localdate().isoformat(),
    })


@login_required
@require_http_methods(["POST"])
def count_create(request):
    c = PhysicalCount.objects.create(
        counted_on=parse_date(request.POST.get("counted_on") or "") or timezone.localdate(),
        label=(request.POST.get("label") or "").strip(),
        operator=request.user)
    return redirect("stock-count", pk=c.pk)


@login_required
@require_http_methods(["GET", "POST"])
def count_detail(request, pk):
    count = get_object_or_404(PhysicalCount, pk=pk)

    if request.method == "POST" and not count.is_committed:
        lines, errors = [], []
        for key, raw in request.POST.items():
            if not key.startswith("qty:") or str(raw).strip() == "":
                continue
            item = _resolve(key[4:])
            if item is None:
                continue
            val = _dec(raw)
            if val is None or val < 0:
                errors.append(f"{item.name}: '{raw}' isn't a quantity.")
                continue
            lines.append((item, val))

        if errors:
            return render(request, "web/stock_count_detail.html",
                          _count_ctx(count, errors="; ".join(errors)), status=400)
        if not lines:
            return render(request, "web/stock_count_detail.html",
                          _count_ctx(count, errors="Nothing was counted — enter at least one line."),
                          status=400)
        try:
            stock_svc.commit_count(count, lines, operator=request.user)
        except ValueError as e:
            return render(request, "web/stock_count_detail.html",
                          _count_ctx(count, errors=str(e)), status=400)
        return redirect("stock-count", pk=count.pk)

    return render(request, "web/stock_count_detail.html", _count_ctx(count))


def _count_ctx(count, errors=None):
    """Count sheet: every catalog item with its book quantity to count against."""
    rows = []
    for key, model in CATALOGS.items():
        for obj in model.objects.order_by("name"):
            rows.append({"key": f"{key}:{obj.pk}", "item": obj, "unit": obj.unit,
                         "kind_label": dict(KIND_LABELS)[key],
                         "book": stock_svc.on_hand(obj),
                         "wac": stock_svc.wac(obj)})
    return {
        "nav": "inventory", "count": count, "rows": rows, "errors": errors,
        "adjustments": count.adjustments.filter(voided_at__isnull=True)
                            .order_by("id") if count.is_committed else [],
        "variance": count.variance_value if count.is_committed else None,
    }
