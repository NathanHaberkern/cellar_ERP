"""Tranche 4: buy labels for a pool, void them, print them.

A pool moves  OPEN -> LABELED -> SHIPPED  and its orders  POOLED -> LABELED ->
FULFILLED.  Statuses are recomputed from the labels (refresh_pool) after every
buy and void rather than set by hand, so a void in the middle of a run puts
exactly the affected orders back where they belong.

Buying a pool's labels is one carrier call per package, each saved as it
comes back. A refusal on package 7 doesn't undo packages 1-6 (they are real
labels, already paid for); it's reported and the rest carry on, and pressing
"Buy labels" again only buys the ones still missing.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shipping.carriers import Address, CarrierError, LabelRequest, Parcel, get_adapter
from shipping.carriers import fedex, gls, ups
from shipping.models import CarrierPickup, Label, Package, PackPool, ShipOrder
from shipping.services.labels import LabelRefused, buy_label, void_label

log = logging.getLogger(__name__)
S = ShipOrder.Status

SERVICE_FOR = {"fedex": fedex.default_service, "ups": ups.default_service,
               "gls": gls.default_service}


def ship_from() -> Address:
    return Address.from_settings(settings.SHIPPING["SHIP_FROM"])


def _ship_to(order: ShipOrder) -> Address:
    # Recipient phone: both carriers want one and C7 often has none. The
    # winery's number is the usual stand-in (it's what Shipment Manager did) --
    # a driver with a question calls us, which is what we want anyway.
    phone = order.ship_phone or settings.SHIPPING["SHIP_FROM"].get("phone", "")
    # No recipient email on purpose: C7 sends the ship confirmation; a second
    # one from the carrier just confuses people.
    return Address(name=order.ship_name, company=order.ship_company,
                   street1=order.ship_street1, street2=order.ship_street2,
                   city=order.ship_city, state=order.ship_state,
                   postal_code=order.ship_postal_code, country=order.ship_country or "US",
                   phone=phone, residential=order.ship_residential)


def label_request_for(package: Package, ship_date=None) -> LabelRequest:
    """Every order in a package shares one address (that's why they share a box),
    so the first order's ship-to is the package's."""
    orders = list(package.orders.order_by("c7_order_number"))
    if not orders:
        raise LabelRefused(f"Package {package.pk} has no orders")
    carrier = package.pool.carrier
    to = _ship_to(orders[0])
    box = package.box
    reference = "/".join(o.c7_order_number for o in orders)
    return LabelRequest(
        ship_from=ship_from(), ship_to=to,
        parcel=Parcel(box.length_in, box.width_in, box.height_in, package.weight_lb,
                      reference=reference),
        service=SERVICE_FOR[carrier](to.residential),
        ship_date=ship_date or timezone.localdate(),
        contains_alcohol=True, adult_signature=True,
    )


def _check_ready(pool: PackPool):
    if not settings.SHIPPING["SHIP_FROM"].get("phone"):
        raise LabelRefused("Set SHIP_FROM_PHONE in .env (both carriers require a shipper phone).")
    changed = [o.c7_order_number for p in pool.packages.all() for o in p.orders.all()
               if o.c7_change_note]
    if changed:
        raise LabelRefused("C7 changed order(s) " + ", ".join(f"#{n}" for n in changed)
                           + " after the pool was built -- dissolve and rebuild first.")


def buy_package_label(package: Package, operator=None, adapter=None) -> Label:
    _check_ready(package.pool)
    label = buy_label(label_request_for(package), carrier=package.pool.carrier,
                      package=package, operator=operator, adapter=adapter)
    refresh_pool(package.pool)
    return label


def buy_pool_labels(pool: PackPool, operator=None, adapter=None):
    """Buy every missing label in the pool. Returns (bought Labels, [(package, error)])."""
    _check_ready(pool)
    adapter = adapter or get_adapter(pool.carrier)
    bought, errors = [], []
    for package in pool.packages.order_by("sequence", "pk"):
        if package.live_label is not None:
            continue
        try:
            bought.append(buy_label(label_request_for(package), carrier=pool.carrier,
                                    package=package, operator=operator, adapter=adapter))
        except (CarrierError, LabelRefused) as e:
            errors.append((package, str(e)))
    refresh_pool(pool)
    return bought, errors


def void_package_label(label: Label, operator=None, adapter=None) -> Label:
    """Void a label. Refused once its tracking has gone to C7: the customer has
    already been emailed that number, so fix it in C7 first (by hand) and the
    email trail stays honest."""
    package = label.package
    if package is not None:
        # In Python, not a JSON `contains` lookup: SQLite (local) doesn't support it.
        posted = [o.c7_order_number for o in package.orders.all()
                  if o.status == S.FULFILLED
                  or any(label.tracking_number in (fp.tracking_numbers or [])
                         for fp in o.fulfillment_posts.filter(succeeded=True))]
        if posted:
            raise LabelRefused(
                f"{label.tracking_number} is already posted to C7 on "
                + ", ".join(f"#{n}" for n in posted)
                + ". Delete that fulfillment in C7 first, then void here.")
    void_label(label, operator=operator, adapter=adapter)
    if package is not None:
        refresh_pool(package.pool)
    return label


@transaction.atomic
def refresh_pool(pool: PackPool) -> PackPool:
    """Recompute order and pool status from the labels and C7 posts."""
    packages = list(pool.packages.prefetch_related("orders", "labels"))
    labeled_pkgs = {p.pk for p in packages
                    if any(lb.voided_at is None for lb in p.labels.all())}
    orders = {}
    for p in packages:
        for o in p.orders.all():
            orders.setdefault(o.pk, (o, []))[1].append(p.pk)
    for o, pkg_ids in orders.values():
        if o.status in (S.FULFILLED, S.CANCELLED):
            continue
        new = S.LABELED if all(pid in labeled_pkgs for pid in pkg_ids) else S.POOLED
        if o.status != new:
            o.status = new
            o.save(update_fields=["status", "synced_at"])

    statuses = {o.status for o, _ in orders.values()} or {S.POOLED}
    if statuses <= {S.FULFILLED, S.CANCELLED}:
        new = PackPool.Status.SHIPPED
    elif packages and len(labeled_pkgs) == len(packages):
        new = PackPool.Status.LABELED
    else:
        new = PackPool.Status.OPEN
    if pool.status != new:
        pool.status = new
        pool.save(update_fields=["status"])
    return pool


# --- pickups -------------------------------------------------------------------

TODAY, LATE, NEXT_DAY = "today", "late", "next_day"


def _hhmm(value: str) -> dt.time:
    h, m = value.split(":")
    return dt.time(int(h), int(m))


def gls_time(t: dt.time) -> str:
    """GLS wants "hh:mm am" (its examples: "12:40 pm", "06:00 pm")."""
    return f"{(t.hour % 12) or 12:02d}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def next_business_day(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def pickup_options(now=None) -> dict:
    """What the pool page offers. Normal: now until 3 PM today. Too late for
    that (GLS needs a 4-hour window, or it's a weekend): 4 PM today if a
    4-hour window still fits, and the next business day."""
    conf = settings.SHIPPING["GLS"]
    now = timezone.localtime(now) if now else timezone.localtime()
    min_window = dt.timedelta(minutes=conf["PICKUP_MIN_WINDOW_MIN"])
    # Earliest = now rounded up to the next 5 minutes.
    start = (now + dt.timedelta(minutes=4)).replace(second=0, microsecond=0)
    start = start.replace(minute=start.minute - start.minute % 5)

    def window(close):
        end = now.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
        return (start.time(), close) if start + min_window <= end else None

    weekday = now.weekday() < 5
    today = window(_hhmm(conf["PICKUP_CLOSE"])) if weekday else None
    late = window(_hhmm(conf["PICKUP_LATE_CLOSE"])) if weekday and not today else None
    nxt = next_business_day(now.date())
    out = {"too_late": today is None, "options": {}}
    if today:
        out["options"][TODAY] = (now.date(), *today)
    if late:
        out["options"][LATE] = (now.date(), *late)
    if not today:
        out["options"][NEXT_DAY] = (nxt, _hhmm(conf["PICKUP_NEXT_DAY_FROM"]),
                                    _hhmm(conf["PICKUP_CLOSE"]))
    return out


def booked_pickup(carrier: str, now=None) -> CarrierPickup | None:
    """A successful pickup for today or later -- one covers all of today's boxes."""
    today = timezone.localtime(now).date() if now else timezone.localdate()
    return (CarrierPickup.objects.filter(carrier=carrier, pickup_date__gte=today,
                                         succeeded=True, voided_at__isnull=True)
            .order_by("pickup_date", "-created_at").first())


def request_pickup(pool: PackPool, option: str = TODAY, operator=None, adapter=None,
                   now=None) -> CarrierPickup:
    """Book a GLS on-call pickup. GLS has no daily stop, so this is only offered
    once a pool has labels, and only once: every GLS box labeled since the last
    pickup (any pool) goes on the one pickup. Every request carries the
    closing-time note (PICKUP_INSTRUCTIONS)."""
    if pool.carrier != "gls":
        raise LabelRefused("Pickups are booked here for GLS only; FedEx and UPS come daily.")
    existing = booked_pickup("gls", now)
    if existing is not None:
        raise LabelRefused(f"GLS pickup already booked for {existing.pickup_date:%a %b} "
                           f"{existing.pickup_date.day} (confirmation {existing.confirmation}).")
    plan = pickup_options(now)
    if option not in plan["options"]:
        raise LabelRefused("Too late for a 3 PM pickup today -- choose 4 PM or the next "
                           "business day." if option == TODAY else
                           "That pickup time isn't available any more -- reload the page.")
    day, earliest, latest = plan["options"][option]
    # The boxes waiting for GLS: live labels bought since the last pickup was
    # booked (at most a week back, so an old forgotten label doesn't count).
    since = timezone.now() - dt.timedelta(days=7)
    last = (CarrierPickup.objects.filter(carrier="gls", succeeded=True, voided_at__isnull=True)
            .order_by("-created_at").first())
    if last is not None and last.created_at > since:
        since = last.created_at
    labels = list(Label.objects.filter(carrier="gls", voided_at__isnull=True,
                                       package__pool__carrier="gls", created_at__gt=since))
    if not any(lb.package.pool_id == pool.pk for lb in labels):
        raise LabelRefused("No GLS labels waiting for pickup in this pool -- buy labels first.")
    conf = settings.SHIPPING["GLS"]
    frm = settings.SHIPPING["SHIP_FROM"]
    pr = gls.PickupRequest(
        pickup_date=day, earliest=gls_time(earliest), latest=gls_time(latest),
        packages=len(labels), total_weight_lb=sum((lb.weight_lb for lb in labels), Decimal(0)),
        company=frm.get("company", ""), contact=frm.get("name", ""),
        phone=frm.get("phone", ""), email=frm.get("email", ""),
        street1=frm.get("street1", ""), street2=frm.get("street2", ""),
        city=frm.get("city", ""), state=frm.get("state", ""),
        postal_code=frm.get("postal_code", ""), instructions=conf["PICKUP_INSTRUCTIONS"])
    adapter = adapter or get_adapter("gls")
    row = dict(carrier="gls", pool=pool, pickup_date=day, packages=pr.packages,
               weight_lb=pr.total_weight_lb, operator=operator,
               notes=f"{pr.earliest}-{pr.latest}")
    try:
        confirmation, body = adapter.request_pickup(pr)
    except CarrierError as e:
        CarrierPickup.objects.create(**row, response_summary={"error": str(e),
                                                              "details": e.details})
        raise
    return CarrierPickup.objects.create(**row, succeeded=True, confirmation=confirmation,
                                        response_summary=body)


# --- printing ------------------------------------------------------------------

def merged_pdf(labels) -> bytes:
    """All documents of the given labels, in order, as one PDF for one print job."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for label in labels:
        for doc in label.documents.all():
            if doc.content_type != "application/pdf":
                continue
            reader = PdfReader(io.BytesIO(bytes(doc.content)))
            for page in reader.pages:
                writer.add_page(page)
    if not writer.pages:
        raise LabelRefused("No label documents to print.")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pool_live_labels(pool: PackPool) -> list[Label]:
    return list(Label.objects.filter(package__pool=pool, voided_at__isnull=True)
                .order_by("package__sequence", "package_id").prefetch_related("documents"))
