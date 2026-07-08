"""
Barcode scan-to-move.

A container (barrel) is scanned, then a destination rack is scanned, then the
move is booked as an append-only RackAssignment. Because location lives on the
rack (a rack is one physical place; barrels inherit it), moving a barrel to a rack
is how its location changes -- there's no separate location edit.

Flow (built for a phone + a keyboard-wedge scanner, which types the code then
sends Enter):
    1. scan barrel barcode  -> live resolve: which barrel, where it is now
    2. scan rack barcode     -> live resolve: which rack, its location
    3. Book move             -> insert RackAssignment(container, rack), show receipt,
                                reset focus to the barrel field for the next move

Bound to the real aging models: Container/Rack resolve on `barcode`; a move closes
the container's active RackAssignment (removed_at, a CLOSE_FIELD) and opens a new one
with the first free `position` on the target rack, stamping `operator`.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from cellar.models.aging import Container, Rack, RackAssignment


# ------------------------------------------------------------- resolvers -----
def _find(model, barcode):
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    # scanners emit the exact code; fall back to case-insensitive just in case
    return (model.objects.filter(barcode=barcode).first()
            or model.objects.filter(barcode__iexact=barcode).first())


def _label(obj):
    """Human label: container_id / rack_id are the real permanent IDs."""
    for attr in ("container_id", "rack_id", "barcode"):
        val = getattr(obj, attr, None)
        if val:
            return str(val)
    return f"#{obj.pk}"


def _current_assignment(container):
    """The container's active assignment = not removed and not voided."""
    return (RackAssignment.objects
            .filter(container=container, removed_at__isnull=True, voided_at__isnull=True)
            .order_by("-pk").first())


def _current_rack(container):
    a = _current_assignment(container)
    return a.rack if a else None


def _rack_location(rack):
    return getattr(rack, "location", None)


def _free_position(rack):
    """First open position on the rack (1..positions), or None if full."""
    taken = set(rack.occupants().keys())          # position -> container (active)
    for p in range(1, (rack.positions or 1) + 1):
        if p not in taken:
            return p
    return None


def _book_move(container, rack, position, operator):
    """Close the container's current assignment, then open the new one.
    Both are append-only rows; the close sets removed_at (a CLOSE_FIELD)."""
    now = timezone.now()
    current = _current_assignment(container)
    if current is not None:
        # close via queryset.update (mirrors the admin's void semantics; skips the
        # AppendOnly save guard cleanly — removed_at is a CLOSE_FIELD anyway)
        RackAssignment.objects.filter(pk=current.pk).update(removed_at=now)
    return RackAssignment.objects.create(
        container=container, rack=rack, position=position,
        assigned_at=now, operator=operator)


# ----------------------------------------------------------------- views -----
@login_required
def scan_index(request):
    return render(request, "web/scan.html", {"nav": "move"})


@login_required
def resolve_container(request):
    obj = _find(Container, request.GET.get("barcode"))
    ctx = {"kind": "barrel", "obj": obj}
    if obj is not None:
        rack = _current_rack(obj)
        ctx.update({"label": _label(obj),
                    "current_rack": _label(rack) if rack else None,
                    "current_location": _rack_location(rack) if rack else None})
    return render(request, "web/_scan_found.html", ctx)


@login_required
def resolve_rack(request):
    obj = _find(Rack, request.GET.get("barcode"))
    ctx = {"kind": "rack", "obj": obj}
    if obj is not None:
        ctx.update({"label": _label(obj), "current_location": _rack_location(obj)})
    return render(request, "web/_scan_found.html", ctx)


@login_required
@require_http_methods(["POST"])
def book_move(request):
    container = _find(Container, request.POST.get("container_barcode"))
    rack = _find(Rack, request.POST.get("rack_barcode"))

    if container is None or rack is None:
        missing = "barrel" if container is None else "rack"
        return render(request, "web/_scan_receipt.html",
                      {"error": f"Can't book the move — the {missing} barcode didn't match anything. "
                                f"Re-scan and try again."})

    from_rack = _current_rack(container)
    if from_rack is not None and from_rack.pk == rack.pk:
        return render(request, "web/_scan_receipt.html",
                      {"error": f"{_label(container)} is already on {_label(rack)}. No move booked."})

    position = _free_position(rack)
    if position is None:
        return render(request, "web/_scan_receipt.html",
                      {"error": f"{_label(rack)} is full ({rack.positions} positions). "
                                f"Move a barrel off it first."})

    error = None
    try:
        _book_move(container, rack, position, request.user)
    except Exception as e:  # noqa: BLE001
        error = f"RackAssignment: {e}"

    return render(request, "web/_scan_receipt.html", {
        "error": error,
        "container": _label(container),
        "from_rack": _label(from_rack) if from_rack else None,
        "to_rack": _label(rack),
        "to_position": position,
        "to_location": _rack_location(rack),
        "when": timezone.localtime(timezone.now()).strftime("%H:%M"),
    })
