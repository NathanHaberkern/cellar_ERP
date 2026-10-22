"""Post tracking back to Commerce7 as a "Shipped" fulfillment.

C7 then marks the order Fulfilled and (sendTransactionEmail) emails the
customer their tracking -- the same email Shipment Manager triggers today.

    POST /order/{id}/fulfillment
    {"type": "Shipped", "sendTransactionEmail": true, "fulfillmentDate": ...,
     "shipped": {"carrier": "FedEx", "trackingNumbers": [...]},
     "packageCount": n, "items": [{"orderItemId": ..., "quantity": ...}]}

The response shape we read back (order.fulfillments[].shipped.trackingNumbers)
is the one shipping_replay already reads from real orders. The request body
follows C7's docs; `orderItemId` = the order line's `id`. Verify on the first
live post (the low-volume test), same as every other C7 field so far.

Safety rules, in order:
  * a SANDBOX label is never posted -- its tracking number is fake and the
    customer would be emailed it;
  * SHIPPING["C7_WRITEBACK"] = "off" posts nothing (dry run with real labels);
  * a post that already succeeded for the same tracking numbers is not repeated;
  * after a post that failed without a clear answer (timeout, 5xx) we first
    ask C7 whether it has the tracking anyway, so a retry never sends a second
    email for the same box.
Every attempt is logged as a FulfillmentPost row, success or not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings
from django.utils import timezone

from integrations.commerce7 import C7Error, Commerce7Client
from shipping.models import CarrierEnvironment, FulfillmentPost, Label, PackPool, ShipOrder
from shipping.services.shipments import refresh_pool

log = logging.getLogger(__name__)
S = ShipOrder.Status


def writeback_on() -> bool:
    return settings.SHIPPING.get("C7_WRITEBACK", "on") == "on"


@dataclass
class PostResult:
    posted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (order number, why)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self):
        parts = [f"{len(self.posted)} posted"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts)


def order_labels(order: ShipOrder) -> list[Label] | None:
    """The live label of every package this order is in, or None if any is missing."""
    labels = []
    for package in order.packages.all().order_by("sequence", "pk"):
        live = package.live_label
        if live is None:
            return None
        labels.append(live)
    return labels or None


def _items(order: ShipOrder) -> list[dict]:
    out = []
    for item in (order.c7_payload or {}).get("items") or []:
        if item.get("hasShipping") is False or not item.get("id"):
            continue
        out.append({"orderItemId": item["id"], "quantity": int(item.get("quantity") or 0)})
    return out


def build_payload(order: ShipOrder, labels: list[Label], send_email=True) -> dict:
    carrier = labels[0].carrier
    return {
        "type": "Shipped",
        "sendTransactionEmail": send_email,
        "fulfillmentDate": timezone.now().isoformat().replace("+00:00", "Z"),
        "shipped": {"carrier": settings.SHIPPING["C7_CARRIER_NAMES"].get(carrier, carrier),
                    "trackingNumbers": [lb.tracking_number for lb in labels]},
        "packageCount": len(labels),
        "items": _items(order),
    }


def _c7_has(client, order: ShipOrder, tracking: list[str]) -> bool:
    raw = client.get(f"order/{order.c7_order_id}")
    have = set()
    for f in raw.get("fulfillments") or []:
        shipped = f.get("shipped") or {}
        have |= {str(n) for n in (shipped.get("trackingNumbers") or f.get("trackingNumbers") or [])}
    return set(tracking) <= have


def _mark_fulfilled(order: ShipOrder):
    order.status = S.FULFILLED
    order.save(update_fields=["status", "synced_at"])


def post_order(order: ShipOrder, *, client=None, operator=None, send_email=True):
    """Post one order. Returns ("posted" | "skipped" | "error", message)."""
    if order.status == S.FULFILLED:
        return "skipped", "already fulfilled"
    labels = order_labels(order)
    if labels is None:
        return "skipped", "not every box has a label yet"
    if any(lb.environment != CarrierEnvironment.PRODUCTION for lb in labels):
        return "skipped", "test (sandbox) label -- never posted to C7"
    if not writeback_on():
        return "skipped", "C7 writeback is off (SHIP_C7_WRITEBACK)"
    tracking = [lb.tracking_number for lb in labels]
    previous = list(order.fulfillment_posts.all())
    if any(p.succeeded and sorted(p.tracking_numbers) == sorted(tracking) for p in previous):
        _mark_fulfilled(order)
        return "skipped", "already posted"

    client = client or Commerce7Client.from_settings()
    carrier = labels[0].carrier
    unclear = [p for p in previous if not p.succeeded
               and (p.http_status is None or p.http_status >= 500)]
    if unclear:
        try:
            if _c7_has(client, order, tracking):
                FulfillmentPost.objects.create(
                    order=order, carrier=carrier, tracking_numbers=tracking,
                    send_email=send_email, http_status=200, succeeded=True, operator=operator,
                    response_summary={"note": "found in C7 after an unclear earlier attempt; "
                                              "not posted again"})
                _mark_fulfilled(order)
                return "posted", "C7 already had it"
        except (C7Error, requests.RequestException) as e:
            return "error", f"couldn't check C7 before retrying: {e}"

    payload = build_payload(order, labels, send_email=send_email)
    try:
        body = client.request("POST", f"order/{order.c7_order_id}/fulfillment", json=payload)
    except C7Error as e:
        FulfillmentPost.objects.create(
            order=order, carrier=carrier, tracking_numbers=tracking, send_email=send_email,
            http_status=e.status_code, succeeded=False, operator=operator,
            response_summary={"error": str(e), "body": e.body if isinstance(e.body, (dict, list))
                              else str(e.body)[:500], "request": payload})
        return "error", str(e)
    except requests.RequestException as e:
        FulfillmentPost.objects.create(
            order=order, carrier=carrier, tracking_numbers=tracking, send_email=send_email,
            http_status=None, succeeded=False, operator=operator,
            response_summary={"error": f"{type(e).__name__}: {e}", "request": payload})
        return "error", f"no answer from C7 ({type(e).__name__}) -- retry checks C7 first"

    FulfillmentPost.objects.create(
        order=order, carrier=carrier, tracking_numbers=tracking, send_email=send_email,
        http_status=200, succeeded=True, operator=operator,
        response_summary={"id": (body or {}).get("id"), "request": payload})
    _mark_fulfilled(order)
    return "posted", ", ".join(tracking)


def post_pool(pool: PackPool, *, client=None, operator=None) -> PostResult:
    result = PostResult()
    orders = (ShipOrder.objects.filter(packages__pool=pool).distinct()
              .order_by("c7_order_number"))
    for order in orders:
        outcome, msg = post_order(order, client=client, operator=operator)
        if outcome == "posted":
            result.posted.append(order.c7_order_number)
        elif outcome == "skipped":
            result.skipped.append((order.c7_order_number, msg))
        else:
            result.errors.append((order.c7_order_number, msg))
    refresh_pool(pool)
    return result
