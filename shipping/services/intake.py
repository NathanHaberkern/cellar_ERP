"""Order intake: C7 -> ShipOrder, address validation, holds and releases.

Status flow for an order before it is packed:

    NEW --validate--> READY ----------------> POOLED (services/pools.py)
      \\--problem--> ADDRESS_REVIEW --accept/keep/edit--> READY
    any open status --hold--> ON_HOLD --release--> NEW or READY

"Held" in the UI covers two different things, on purpose kept apart:
  * ON_HOLD is a status someone set by hand, with a note.
  * hold_reasons() are computed every time from the data: a future requested
    ship date, unknown bottle size, or (once the compliance gate is on) a
    non-Compliant C7 status. These lift by themselves when the cause goes away,
    so nobody has to remember to release an order on its ship date.

Re-sync never rewrites an order that is already pooled or labeled. If C7
changed it after that point, c7_change_note says so and a person decides.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.commerce7 import C7Error, Commerce7Client
from shipping.carriers import Address, CarrierConfigError, CarrierError, get_adapter
from shipping.models import ShipOrder, ShipOrderLine, SkuShippingProfile
from shipping.services.packing import (LineData, OrderData, address_key, norm_text,
                                       order_from_c7, zip5)

log = logging.getLogger(__name__)
S = ShipOrder.Status
OPEN_EDITABLE = (S.NEW, S.ADDRESS_REVIEW, S.READY, S.ON_HOLD)
COMPLIANT = "Compliant"


# --- bottle size ---------------------------------------------------------------

def resolve_ml(sku: str, c7_ml) -> tuple[int | None, int]:
    """(ml, bottles_per_unit) for a SKU.

    Order of trust: a SkuShippingProfile row (Nate's explicit override) beats
    C7's volumeInML (present on every line on our tenant), which beats the
    ERP's own BottleFormat for that SKU's bottling run. None = unknown, and the
    order is held until someone sets it.
    """
    profile = SkuShippingProfile.objects.filter(sku=sku).first()
    if profile:
        return profile.ml, profile.bottles_per_unit
    if c7_ml:
        return int(c7_ml), 1
    try:
        from cellar.models import BottlingRun
        run = (BottlingRun.objects.filter(sku=sku).select_related("bottle_format")
               .order_by("-pk").first())
        fmt = getattr(run, "bottle_format", None) if run else None
        if fmt:
            return fmt.ml, 1
    except Exception:            # cellar schema drift must never break shipping intake
        log.exception("BottleFormat lookup failed for %s", sku)
    return None, 1


# --- C7 sync -------------------------------------------------------------------

@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    readdressed: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self):
        parts = [f"{len(self.created)} new", f"{len(self.updated)} updated"]
        if self.readdressed:
            parts.append(f"{len(self.readdressed)} address changed in C7")
        if self.closed:
            parts.append(f"{len(self.closed)} closed in C7")
        if self.flagged:
            parts.append(f"{len(self.flagged)} changed after packing — check")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


def _trim_payload(o: dict) -> dict:
    """Keep the order, drop the embedded customer record (purchase history,
    marketing flags) -- it's ~50 KB per order and none of it is shipping data."""
    out = {k: v for k, v in o.items() if k != "customer"}
    cust = o.get("customer") or {}
    out["customer"] = {"id": cust.get("id"), "customerType": cust.get("customerType")}
    return out


def _email(o: dict) -> str:
    for e in ((o.get("customer") or {}).get("emails") or []):
        if e.get("email"):
            return e["email"]
    return ""


def _address_fields(d: OrderData, raw: dict) -> dict:
    ship = raw.get("shipTo") or {}
    return {
        "ship_name": d.name[:120], "ship_company": d.company[:120],
        "ship_street1": d.street1[:120], "ship_street2": d.street2[:120],
        "ship_city": d.city[:80], "ship_state": d.state[:2],
        "ship_postal_code": d.postal_code[:10], "ship_country": (d.country or "US")[:2],
        "ship_phone": (ship.get("phone") or "")[:30], "ship_email": _email(raw)[:254],
    }


def _lines_sig(lines):
    return sorted((x.sku, x.quantity) for x in lines)


def _write_lines(order: ShipOrder, d: OrderData):
    order.lines.all().delete()
    ShipOrderLine.objects.bulk_create([
        ShipOrderLine(order=order, sku=x.sku[:60], title=x.title[:200], quantity=x.quantity,
                      ml=x.ml, bottles_per_unit=x.bottles_per_unit, is_port_club=x.is_port_club)
        for x in d.lines])


def _same_address(order: ShipOrder, d: OrderData) -> bool:
    """Compare C7's current ship-to with what C7 sent last time (address_original
    if a reviewer changed it, so accepting a FedEx fix doesn't look like a C7 edit)."""
    base = order.address_original or order.address_dict()
    prev = OrderData(key="", number="", name=base.get("ship_name", ""),
                     street1=base.get("ship_street1", ""), street2=base.get("ship_street2", ""),
                     city=base.get("ship_city", ""), state=base.get("ship_state", ""),
                     postal_code=base.get("ship_postal_code", ""))
    return address_key(prev) == address_key(d) and norm_text(prev.name) == norm_text(d.name)


def upsert_order(raw: dict, result: SyncResult):
    d = order_from_c7(raw, ml_for_sku=resolve_ml)
    meta = {
        "c7_order_number": d.number, "c7_customer_id": d.customer_id,
        "is_club": d.is_club, "club_name": d.club_name[:120],
        "club_package": d.club_package[:120], "compliance_status": d.compliance_status[:40],
        "requested_ship_date": d.requested_ship_date, "c7_payload": _trim_payload(raw),
    }
    paid = raw.get("orderPaidDate") or raw.get("orderSubmittedDate")
    order = ShipOrder.objects.filter(c7_order_id=d.key).first()

    if order is None:
        order = ShipOrder.objects.create(c7_order_id=d.key, ordered_at=paid, status=S.NEW,
                                         **meta, **_address_fields(d, raw))
        _write_lines(order, d)
        result.created.append(d.number)
        return order

    if order.status in OPEN_EDITABLE:
        for k, v in meta.items():
            setattr(order, k, v)
        if not _same_address(order, d):
            for k, v in _address_fields(d, raw).items():
                setattr(order, k, v)
            order.address_original, order.address_suggested, order.address_issues = {}, {}, []
            order.address_validated_at = None
            order.ship_residential = None
            order.address_classification = ""
            if order.status != S.ON_HOLD:
                order.status = S.NEW
            result.readdressed.append(d.number)
        if _lines_sig(order.lines.all()) != _lines_sig(d.lines):
            _write_lines(order, d)
        order.c7_change_note = ""
        order.save()
        result.updated.append(d.number)
        return order

    # Pooled / labeled / fulfilled: never rewrite, only flag differences.
    changes = []
    if not _same_address(order, d):
        changes.append("address")
    if _lines_sig(order.lines.all()) != _lines_sig(d.lines):
        changes.append("items")
    if changes and order.status in (S.POOLED, S.LABELED):
        note = f"C7 changed {' and '.join(changes)} after packing started"
        if order.c7_change_note != note:
            order.c7_change_note = note
            order.save(update_fields=["c7_change_note", "synced_at"])
        result.flagged.append(d.number)
    order.compliance_status = d.compliance_status[:40]
    order.save(update_fields=["compliance_status", "synced_at"])
    return order


def _close_missing(client, seen_ids, result: SyncResult):
    """Open orders that didn't come back as unfulfilled: ask C7 what happened."""
    candidates = ShipOrder.objects.filter(
        status__in=OPEN_EDITABLE + (S.POOLED,)).exclude(c7_order_id__in=seen_ids)
    for order in candidates:
        try:
            raw = client.get(f"order/{order.c7_order_id}")
        except C7Error as e:
            if e.status_code == 404:
                raw = {"fulfillmentStatus": "Deleted"}
            else:
                result.errors.append(f"#{order.c7_order_number}: {e}")
                continue
        status = raw.get("fulfillmentStatus") or ""
        if status == "Not Fulfilled" and raw.get("orderDeliveryMethod", "Ship") == "Ship":
            continue                         # just older than the lookback window
        if order.status == S.POOLED:
            order.c7_change_note = f"No longer open in C7 ({status or 'changed'}) — pull from pool"
            order.save(update_fields=["c7_change_note", "synced_at"])
            result.flagged.append(order.c7_order_number)
            continue
        order.status = S.FULFILLED if status == "Fulfilled" else S.CANCELLED
        order.c7_change_note = f"Closed in C7 outside this app ({status or 'changed'})"
        order.save(update_fields=["status", "c7_change_note", "synced_at"])
        result.closed.append(order.c7_order_number)


def sync_orders(client: Commerce7Client | None = None, *, since: date | None = None,
                today: date | None = None) -> SyncResult:
    """Pull every unfulfilled ship order paid since `since` and upsert it."""
    client = client or Commerce7Client.from_settings()
    today = today or timezone.localdate()
    since = since or today - timedelta(days=settings.SHIPPING["SYNC_LOOKBACK_DAYS"])
    params = {"orderPaidDate": f"gte:{since.isoformat()}", "orderDeliveryMethod": "Ship",
              "fulfillmentStatus": "Not Fulfilled"}
    result = SyncResult()
    seen = []
    for raw in client.iter_pages("order", "orders", params):
        seen.append(str(raw.get("id")))
        try:
            with transaction.atomic():
                upsert_order(raw, result)
        except Exception as e:             # one bad order must not stop the run
            log.exception("sync failed for C7 order %s", raw.get("orderNumber"))
            result.errors.append(f"#{raw.get('orderNumber')}: {e}")
    _close_missing(client, seen, result)
    return result


# --- address validation --------------------------------------------------------

def _as_address(order: ShipOrder) -> Address:
    return Address(name=order.ship_name, company=order.ship_company,
                   street1=order.ship_street1, street2=order.ship_street2,
                   city=order.ship_city, state=order.ship_state,
                   postal_code=order.ship_postal_code, country=order.ship_country,
                   phone=order.ship_phone, email=order.ship_email)


def _material_differences(order: ShipOrder, sug: Address) -> list[str]:
    """What FedEx changed that a person should see. Formatting (case, 'Street'
    vs 'ST', ZIP+4) is not a change; a different street, city, unit or ZIP5 is."""
    diffs = []
    mine = norm_text(f"{order.ship_street1} {order.ship_street2}").replace(" ", "")
    theirs = norm_text(f"{sug.street1} {sug.street2}").replace(" ", "")
    if mine != theirs:
        diffs.append(f"Street: '{sug.street1} {sug.street2}'".replace(" '", " '").strip())
    if norm_text(order.ship_city) != norm_text(sug.city):
        diffs.append(f"City: {sug.city}")
    if (order.ship_state or "").upper() != (sug.state or "").upper():
        diffs.append(f"State: {sug.state}")
    if zip5(order.ship_postal_code) != zip5(sug.postal_code):
        diffs.append(f"ZIP: {sug.postal_code}")
    return diffs


def validate_order(order: ShipOrder, adapter=None) -> ShipOrder:
    """FedEx check for one NEW order. Clean -> READY; anything doubtful -> ADDRESS_REVIEW."""
    adapter = adapter or get_adapter("fedex")
    try:
        v = adapter.validate_address(_as_address(order))
    except CarrierConfigError:
        raise                    # bad credentials: stop the run, don't flood the review queue
    except CarrierError as e:
        order.address_issues = [f"FedEx validation failed: {e}"]
        order.address_suggested = {}
        order.status = S.ADDRESS_REVIEW
        order.save()
        return order

    order.address_classification = v.classification
    order.ship_residential = v.residential
    order.address_validated_at = timezone.now()
    issues = list(v.issues)
    diffs = _material_differences(order, v.suggested) if v.suggested else []
    if diffs:
        issues = [f"FedEx suggests a different address — {'; '.join(diffs)}"] + issues
        order.address_suggested = {
            "ship_street1": v.suggested.street1, "ship_street2": v.suggested.street2,
            "ship_city": v.suggested.city, "ship_state": v.suggested.state,
            "ship_postal_code": v.suggested.postal_code, "ship_country": v.suggested.country}
    else:
        order.address_suggested = {}
    order.address_issues = issues
    order.status = S.ADDRESS_REVIEW if (issues or not v.resolved) else S.READY
    order.save()
    return order


def validate_new_orders(adapter=None) -> dict:
    counts = {"ready": 0, "review": 0}
    adapter = adapter or get_adapter("fedex")
    for order in ShipOrder.objects.filter(status=S.NEW):
        validate_order(order, adapter)
        counts["ready" if order.status == S.READY else "review"] += 1
    return counts


# --- review actions --------------------------------------------------------------

def _remember_original(order):
    if not order.address_original:
        order.address_original = order.address_dict()


def accept_suggestion(order: ShipOrder, user):
    if not order.address_suggested:
        raise ValueError("No FedEx suggestion to accept")
    _remember_original(order)
    for k, v in order.address_suggested.items():
        setattr(order, k, v)
    _finish_review(order, user)


def keep_address(order: ShipOrder, user):
    _finish_review(order, user)


def edit_address(order: ShipOrder, user, fields: dict, adapter=None):
    """Hand edit, then re-check with FedEx (back to review if still doubtful)."""
    _remember_original(order)
    for k in ShipOrder.ADDRESS_FIELDS:
        if k in fields:
            setattr(order, k, (fields[k] or "").strip())
    order.address_reviewed_by = user
    order.status = S.NEW
    order.save()
    return validate_order(order, adapter)


def set_carrier_override(order: ShipOrder, value: str):
    if value not in ("", "fedex", "ups"):
        raise ValueError(f"Unknown carrier {value!r}")
    order.carrier_override = value
    order.save(update_fields=["carrier_override", "synced_at"])


def set_residential(order: ShipOrder, value: bool | None):
    order.ship_residential = value
    order.save(update_fields=["ship_residential", "synced_at"])


def _finish_review(order, user):
    order.address_issues = []
    order.address_suggested = {}
    order.address_reviewed_by = user
    order.status = S.READY
    order.save()


# --- holds -------------------------------------------------------------------------

def compliance_gate_on() -> bool:
    return settings.SHIPPING.get("COMPLIANCE_GATE", "off") == "on"


def hold_reasons(order: ShipOrder, today: date | None = None) -> list[str]:
    """Computed reasons this order can't be pooled today (empty = good to go)."""
    today = today or timezone.localdate()
    reasons = []
    if order.requested_ship_date and order.requested_ship_date > today:
        reasons.append(f"Ships on {order.requested_ship_date:%b} {order.requested_ship_date.day}")
    if (compliance_gate_on() and order.compliance_status != COMPLIANT
            and not order.compliance_override_at):
        reasons.append(f"Compliance: {order.compliance_status or 'unknown'}")
    unknown = [ln.sku for ln in order.lines.all() if ln.ml is None]
    if unknown:
        reasons.append(f"Bottle size unknown: {', '.join(unknown)}")
    if not order.lines.exists():
        reasons.append("No shippable items")
    return reasons


def put_on_hold(order: ShipOrder, note: str):
    if order.status not in OPEN_EDITABLE:
        raise ValueError("Only unpooled orders can be put on hold")
    order.hold_note = (note or "On hold")[:200]
    order.status = S.ON_HOLD
    order.save()


def release_hold(order: ShipOrder):
    order.hold_note = ""
    order.status = S.READY if order.address_validated_at and not order.address_issues else S.NEW
    order.save()


def override_compliance(order: ShipOrder, user, note: str):
    if not note.strip():
        raise ValueError("A note is required to release a compliance hold")
    order.compliance_override_by = user
    order.compliance_override_at = timezone.now()
    order.compliance_override_note = note.strip()[:200]
    order.save()


# --- adapters to the pure layer ------------------------------------------------------

def to_order_data(order: ShipOrder) -> OrderData:
    return OrderData(
        key=str(order.pk), number=order.c7_order_number, name=order.ship_name,
        company=order.ship_company, street1=order.ship_street1, street2=order.ship_street2,
        city=order.ship_city, state=order.ship_state, postal_code=order.ship_postal_code,
        country=order.ship_country, is_club=order.is_club, club_name=order.club_name,
        club_package=order.club_package, compliance_status=order.compliance_status,
        requested_ship_date=order.requested_ship_date, customer_id=order.c7_customer_id,
        carrier_override=order.carrier_override,
        lines=[LineData(sku=ln.sku, title=ln.title, quantity=ln.quantity, ml=ln.ml,
                        bottles_per_unit=ln.bottles_per_unit) for ln in order.lines.all()])
