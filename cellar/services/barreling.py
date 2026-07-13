"""
Barreling — racking a lot down to oak. An AGING move, not a lifecycle gate.

WHAT CHANGED AND WHY
--------------------
The old `fermentation.rack_to_barrel()` did two wrong things:

  1. It flipped the lot to DONE_PRIMARY. That made oak a mandatory station on the
     way out of primary, which stranded every wine that never sees a barrel. Status
     now moves on the book-to-bond declaration (see `bonding.py`); this module does
     not touch `lot.status` at all.

  2. It took a TOTAL volume and divided it evenly across the selected barrels:

         per = Decimal(total_volume_gal) / Decimal(len(ids))

     That is exactly backwards for a tank with no pressure sensor, where the
     barrel-down IS the gauge. You cannot derive the fills from a volume you do not
     know — you derive the volume from the fills. So this module takes PER-BARREL
     actuals (prefilled from `Container.capacity_gal`, edited down for the partial)
     and sums them.

PARTIAL BARREL-DOWNS
--------------------
A lot often goes down over several sessions — you fill what barrels you have and
leave the remainder in tank until more free up. So `tank_disposition` is explicit:

  * REMAINS  — the tank assignment stays OPEN. Nothing is lost, nothing is declared.
               The wine is simply split between oak and tank. Come back and rack the
               rest later; `bonding.barrel_fill_total()` sums every open placement,
               so the gauge converges as the lot goes down.
  * EMPTIED  — the tank assignment closes. Anything left behind (lees, heel) is a
               real loss and is booked as a VolumeLoss, which is what 5120.17 reads.

We only ever record a VolumeLoss on EMPTIED, and only for gallons the cellar
explicitly enters. We do not silently infer a loss from a tank volume we may not
have measured — a fabricated loss on a 5120.17 is worse than no loss at all.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from cellar.models import (AgingPlacement, Container, TankAssignment, VolumeLoss)


class TankDisposition:
    REMAINS = "remains"     # partial down — wine still in tank, assignment stays open
    EMPTIED = "emptied"     # tank is done — close it; any shortfall is lees/heel


def empty_oak_containers():
    """Active barrels/foudres with no open placement — what you can rack into.

    NOTE: at real winery scale (hundreds to low-thousands of barrels) this can be
    a very large queryset. It exists for callers that genuinely need the full set
    (e.g. a report); the barrel-down UI does NOT render this directly — see
    `search_empty_oak_containers` / `find_empty_oak_container` below, which are
    the bounded, filter-or-scan entry points the picker actually uses.
    """
    open_ids = set(
        AgingPlacement.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("container_id", flat=True))
    return (Container.objects
            .filter(active=True, type__in=[Container.Type.BARREL, Container.Type.FOUDRE])
            .exclude(id__in=open_ids)
            .order_by("container_id"))


# Cap on any one search/browse result. The picker requires a filter (text search
# and/or type/format) before it will show results at all — see
# `search_empty_oak_containers` — so this is a safety backstop, not the primary
# scale control. Chosen so a result list is still one glance, not a scroll.
SEARCH_LIMIT = 30


def empty_oak_qs():
    """Base queryset — same rows as `empty_oak_containers`, factored out so the
    search/scan helpers below don't duplicate the open-placement exclusion."""
    open_ids = set(
        AgingPlacement.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("container_id", flat=True))
    return (Container.objects
            .filter(active=True, type__in=[Container.Type.BARREL, Container.Type.FOUDRE])
            .exclude(id__in=open_ids))


def empty_oak_formats():
    """Distinct format strings among currently-empty oak containers, for a
    filter dropdown. Blank formats excluded; sorted for a stable list."""
    return sorted({
        f for f in empty_oak_qs().exclude(format="")
                                  .values_list("format", flat=True).distinct()
    })


def search_empty_oak_containers(q=None, type=None, fmt=None, limit=SEARCH_LIMIT):
    """Filtered, bounded lookup for the barrel-down picker.

    Requires at least one of q/type/fmt — an empty call returns nothing. This
    is deliberate: at 1000+ barrels, "browse everything" is not a workable UI,
    so the picker always narrows first (search text, or a type/format filter,
    or a scanned exact ID) rather than ever rendering the full empty set.

    q matches container_id (icontains) or barcode (exact) — a scanner-wedge
    barcode will usually be an exact barcode match; typed text is usually a
    partial container_id.

    Returns up to `limit` rows plus a `total` count so the UI can say
    "37 more — narrow your search" instead of silently truncating.
    """
    q = (q or "").strip()
    if not (q or type or fmt):
        return {"rows": [], "total": 0, "truncated": False}

    qs = empty_oak_qs()
    if type:
        qs = qs.filter(type=type)
    if fmt:
        qs = qs.filter(format=fmt)
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(container_id__icontains=q) | Q(barcode__iexact=q))
    qs = qs.order_by("container_id")

    total = qs.count()
    rows = list(qs[:limit])
    return {"rows": rows, "total": total, "truncated": total > len(rows)}


def find_empty_oak_container(code):
    """Exact-match resolve for scan-to-add: a scanner types the barcode (or the
    operator types a full container ID) and hits Enter. Returns a single
    Container or None — never a list, so the caller can add-on-Enter with no
    intermediate 'pick from results' step. Returns None (not the barrel) if it
    exists but is currently occupied, so the caller can give a clear reason
    rather than silently failing to add it."""
    code = (code or "").strip()
    if not code:
        return None
    return (empty_oak_qs().filter(container_id__iexact=code).first()
            or empty_oak_qs().filter(barcode__iexact=code).first())


def parse_fills(post):
    """Pull per-barrel fills out of the POST.

    The form posts `fill_<container_pk>` for every checked barrel, prefilled with
    that container's capacity and edited down for a partial. A checked barrel with a
    blank or zero volume falls back to its capacity — a barrel you selected but did
    not gauge is assumed full, which is the common case (N full + one partial).
    """
    fills = []
    for cid in post.getlist("containers"):
        cid = str(cid).strip()
        if not cid:
            continue
        container = Container.objects.get(pk=int(cid))
        raw = (post.get(f"fill_{cid}") or "").strip()
        try:
            vol = Decimal(raw) if raw else None
        except Exception:  # noqa: BLE001
            vol = None
        if vol is None or vol <= 0:
            vol = container.capacity_gal or Decimal("0")
        fills.append({"container": container, "volume_gal": Decimal(vol)})
    return fills


@transaction.atomic
def rack_to_barrel(lot, *, fills, filled_at=None, tank_disposition=TankDisposition.REMAINS,
                   lees_gal=None, actor=None):
    """Rack a lot down to oak. Returns the total gallons barreled in THIS session.

    `fills` is [{container, volume_gal}] — actual gallons into each barrel.
    Does NOT change `lot.status`. Booking to bond is a separate, deliberate act.
    """
    filled_at = filled_at or timezone.localdate()
    if not fills:
        raise ValueError("Select at least one barrel to rack into.")

    session_total = Decimal("0")
    for f in fills:
        vol = Decimal(str(f["volume_gal"]))
        if vol <= 0:
            raise ValueError(
                f"{f['container'].container_id}: enter the gallons that actually "
                f"went in — this fill is part of the gauge.")
        AgingPlacement.objects.create(
            lot=lot, container=f["container"], filled_at=filled_at,
            volume_gal=vol.quantize(Decimal("0.1")))
        session_total += vol

    if tank_disposition == TankDisposition.EMPTIED:
        (TankAssignment.objects
         .filter(lot=lot, voided_at__isnull=True, emptied_at__isnull=True)
         .update(emptied_at=timezone.now()))
        if lees_gal not in (None, ""):
            loss = Decimal(str(lees_gal))
            if loss > 0:
                VolumeLoss.objects.create(
                    lot=lot, volume_gal=loss.quantize(Decimal("0.1")),
                    reason="Lees / tank heel at barrel-down",
                    occurred_at=filled_at)

    return session_total.quantize(Decimal("0.1"))
