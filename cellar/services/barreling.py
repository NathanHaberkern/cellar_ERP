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
    """Active barrels/foudres with no open placement — what you can rack into."""
    open_ids = set(
        AgingPlacement.objects
        .filter(emptied_at__isnull=True, voided_at__isnull=True)
        .values_list("container_id", flat=True))
    return (Container.objects
            .filter(active=True, type__in=[Container.Type.BARREL, Container.Type.FOUDRE])
            .exclude(id__in=open_ids)
            .order_by("container_id"))


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
