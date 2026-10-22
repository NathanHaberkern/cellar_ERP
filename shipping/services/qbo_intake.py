"""QuickBooks (wholesale) orders that ship GLS -> ShipOrder, and tracking back.

Which QBO documents come in: Invoices and Sales Receipts dated in the last
SHIPPING["QBO_LOOKBACK_DAYS"] whose Ship via (ShipMethodRef) is one of
SHIPPING["QBO_GLS_SHIP_METHODS"] and that have no tracking number yet. QBO has
no "fulfilled" flag, so an empty TrackingNum is the "not shipped" signal -- and
writing TrackingNum back after labels (write_tracking) is what closes it.

From there a QBO order is an ordinary ShipOrder (source=qbo): it goes through
the same address check, holds, pools, labels and print page as a C7 order,
always on GLS (carrier_override="gls"), in GLS-only pools.

What our QBO data looks like (read Oct 2026):
  * ShipAddr is usually FreeFormAddress: Line1..Line5 with the last line
    "City, ST  ZIP" and the customer/attention name in the first lines.
  * Lines are sold by the CASE (Qty 1 of 23MFZ = 12 bottles); descriptions
    often say "12 x 500 ml". CA CRV and fee lines post to other accounts and
    are skipped (only SHIPPING["QBO_SHIP_ACCOUNTS"] income accounts are boxed).
  * Item names are the same SKUs as Commerce7.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.qbo import QBOClient, QBOError
from shipping.models import CarrierEnvironment, FulfillmentPost, ShipOrder, ShipOrderLine
from shipping.services.intake import OPEN_EDITABLE, resolve_ml

log = logging.getLogger(__name__)
S = ShipOrder.Status
ENTITIES = ("Invoice", "SalesReceipt")

CITY_LINE_RE = re.compile(
    r"^(?P<city>[A-Za-z .'-]+?),?\s+(?P<state>[A-Z]{2})\.?\s+(?P<zip>\d{5})(?:-\d{4})?"
    r"(?:\s+USA?)?\s*$")
STREET_RE = re.compile(r"^\s*\d+\s+\S|^\s*(P\.?\s*O\.?\s*Box)\b", re.I)
UNIT_RE = re.compile(r"^\s*(suite|ste\.?|apt\.?|unit|#|bldg\.?|floor|fl\.?)\b", re.I)
PACK_RE = re.compile(r"(\d+)\s*[xX×]\s*(\d{3,4})\s*ml", re.I)


def qbo_key(entity: str, txn_id: str) -> str:
    return f"qbo:{entity}:{txn_id}"


# --- reading a QBO document -----------------------------------------------------------

def is_gls(doc: dict) -> bool:
    ref = doc.get("ShipMethodRef") or {}
    names = set(settings.SHIPPING["QBO_GLS_SHIP_METHODS"])
    return any((ref.get(k) or "").strip().upper() in names for k in ("name", "value"))


def parse_address(addr: dict, customer: str = "") -> tuple[dict, list[str]]:
    """QBO ShipAddr -> our ship_* fields, plus problems for the review queue.

    Structured addresses (City/CountrySubDivisionCode filled) are taken as is.
    Free-form ones are read line by line: the "City, ST ZIP" line, street
    lines (start with a number or PO Box), unit lines (Suite/Ste/#...), and
    anything before the street is the attention / company name.
    """
    addr = addr or {}
    lines = [addr.get(f"Line{i}", "").strip() for i in range(1, 6)]
    lines = [ln for ln in lines if ln]
    out = {"ship_name": "", "ship_company": customer[:120], "ship_street1": "",
           "ship_street2": "", "ship_city": "", "ship_state": "", "ship_postal_code": "",
           "ship_country": "US"}
    if addr.get("City") and addr.get("CountrySubDivisionCode"):
        names = [ln for ln in lines if not STREET_RE.match(ln) and not UNIT_RE.match(ln)]
        streets = [ln for ln in lines if ln not in names]
        out.update(ship_city=addr["City"], ship_state=addr["CountrySubDivisionCode"].upper()[:2],
                   ship_postal_code=(addr.get("PostalCode") or "")[:10])
    else:
        city_idx = next((i for i in range(len(lines) - 1, -1, -1)
                         if CITY_LINE_RE.match(lines[i])), None)
        if city_idx is None:
            return out, ["Couldn't read the QuickBooks ship-to (no 'City, ST ZIP' line)"]
        m = CITY_LINE_RE.match(lines[city_idx])
        out.update(ship_city=m["city"].strip(), ship_state=m["state"],
                   ship_postal_code=m["zip"])
        before = lines[:city_idx]
        first_street = next((i for i, ln in enumerate(before) if STREET_RE.match(ln)), None)
        if first_street is None:
            # e.g. "Tuck's Inc." / "780 7th Street" has a number, but "One Market Plaza" won't
            names, streets = before[:1], before[1:]
        else:
            names, streets = before[:first_street], before[first_street:]
    issues = []
    if not streets:
        issues.append("No street line in the QuickBooks ship-to")
    out["ship_street1"] = (streets[0] if streets else "")[:120]
    out["ship_street2"] = " ".join(streets[1:])[:120]
    # Attention: a person/store name line that isn't just the customer again.
    attention = next((n for n in names if n.strip().upper() != customer.strip().upper()), "")
    out["ship_name"] = (attention or customer)[:120]
    if attention and not customer:
        out["ship_company"] = ""
    return out, issues


def _account_ok(line: dict) -> bool:
    acct = ((line.get("SalesItemLineDetail") or {}).get("ItemAccountRef") or {}).get("name", "")
    prefixes = settings.SHIPPING["QBO_SHIP_ACCOUNTS"]
    return not prefixes or any(acct.startswith(p) for p in prefixes)


def _known_ml(sku: str) -> int:
    """Bottle size for a QBO line that doesn't state it: SkuShippingProfile /
    ERP bottling run, then what Commerce7 said for the same SKU on an earlier
    order, then 750 ml (nearly all wholesale; a 500 ml Port line normally says
    "12 x 500 ml" in its description and never gets here)."""
    ml, _ = resolve_ml(sku, None)
    if ml:
        return ml
    seen = (ShipOrderLine.objects.filter(sku=sku, ml__isnull=False,
                                         order__source=ShipOrder.Source.C7)
            .values_list("ml", flat=True).first())
    return seen or 750


def doc_lines(doc: dict) -> list[dict]:
    """Shippable lines: [{sku, title, quantity, ml, bottles_per_unit}]."""
    out = []
    per_case = settings.SHIPPING["QBO_BOTTLES_PER_CASE"]
    for line in doc.get("Line") or []:
        if line.get("DetailType") != "SalesItemLineDetail" or not _account_ok(line):
            continue
        d = line["SalesItemLineDetail"]
        sku = (d.get("ItemRef") or {}).get("name") or ""
        qty = d.get("Qty") or 0
        if not sku or not qty:
            continue
        desc = line.get("Description") or ""
        m = PACK_RE.search(desc)
        if m:
            bottles, ml = int(m[1]), int(m[2])
        else:
            ml, bottles = _known_ml(sku), per_case
        qty = float(qty)
        if qty.is_integer():
            quantity, per_unit = int(qty), bottles
        else:                                   # half case etc.: count bottles instead
            quantity, per_unit = round(qty * bottles), 1
        out.append({"sku": sku[:60], "title": (desc or sku)[:200], "ml": ml,
                    "quantity": quantity, "bottles_per_unit": per_unit})
    return out


# --- sync ---------------------------------------------------------------------------

@dataclass
class QBOSyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self):
        parts = [f"{len(self.created)} new GLS", f"{len(self.updated)} updated"]
        for name in ("flagged", "closed", "errors"):
            if getattr(self, name):
                parts.append(f"{len(getattr(self, name))} {name}")
        return ", ".join(parts)


def _fields(entity, doc):
    customer = (doc.get("CustomerRef") or {}).get("name", "")
    addr, issues = parse_address(doc.get("ShipAddr") or doc.get("BillAddr") or {}, customer)
    meta = {
        "source": ShipOrder.Source.QBO, "c7_order_number": (doc.get("DocNumber") or doc["Id"])[:32],
        "ship_email": ((doc.get("BillEmail") or {}).get("Address") or "")[:254],
        "c7_customer_id": f"qbo:{(doc.get('CustomerRef') or {}).get('value', '')}"[:64],
        "compliance_status": "Wholesale (QBO)",
        "carrier_override": "gls",
        "requested_ship_date": _date(doc.get("ShipDate")),
        "c7_payload": {"entity": entity, "Id": doc["Id"], "SyncToken": doc.get("SyncToken"),
                       "DocNumber": doc.get("DocNumber"), "TxnDate": doc.get("TxnDate"),
                       "ShipMethodRef": doc.get("ShipMethodRef"), "ShipAddr": doc.get("ShipAddr"),
                       "CustomerRef": doc.get("CustomerRef")},
    }
    return meta, addr, issues


def _date(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _write_lines(order, lines):
    order.lines.all().delete()
    ShipOrderLine.objects.bulk_create([ShipOrderLine(order=order, **ln) for ln in lines])


def upsert_doc(entity: str, doc: dict, result: QBOSyncResult):
    key = qbo_key(entity, doc["Id"])
    meta, addr, issues = _fields(entity, doc)
    lines = doc_lines(doc)
    order = ShipOrder.objects.filter(c7_order_id=key).first()
    number = meta["c7_order_number"]
    if order is None:
        order = ShipOrder.objects.create(
            c7_order_id=key, ordered_at=None, status=S.ADDRESS_REVIEW if issues else S.NEW,
            address_issues=issues, ship_residential=False, **meta, **addr)
        _write_lines(order, lines)
        result.created.append(number)
        return order
    if order.status in OPEN_EDITABLE:
        changed_addr = any(getattr(order, k) != v for k, v in addr.items()) \
            and not order.address_original
        for k, v in meta.items():
            setattr(order, k, v)
        if changed_addr:
            for k, v in addr.items():
                setattr(order, k, v)
            order.address_issues, order.address_validated_at = issues, None
            if order.status != S.ON_HOLD:
                order.status = S.ADDRESS_REVIEW if issues else S.NEW
        order.save()
        _write_lines(order, lines)
        result.updated.append(number)
        return order
    if order.status in (S.POOLED, S.LABELED):
        prev = sorted((ln.sku, ln.quantity) for ln in order.lines.all())
        if prev != sorted((ln["sku"], ln["quantity"]) for ln in lines):
            order.c7_change_note = "QuickBooks changed items after packing started"
            order.save(update_fields=["c7_change_note", "synced_at"])
            result.flagged.append(number)
    return order


def sync_qbo_orders(client: QBOClient | None = None, *, today: date | None = None,
                    since: date | None = None) -> QBOSyncResult:
    client = client or QBOClient()
    today = today or timezone.localdate()
    since = since or today - timedelta(days=settings.SHIPPING["QBO_LOOKBACK_DAYS"])
    result = QBOSyncResult()
    seen = set()
    for entity in ENTITIES:
        # ShipMethodRef can't be filtered in a QBO query, so filter here.
        for doc in client.query(f"SELECT * FROM {entity} WHERE TxnDate >= '{since.isoformat()}'",
                                entity):
            if not is_gls(doc) or (doc.get("TrackingNum") or "").strip():
                continue
            seen.add(qbo_key(entity, doc["Id"]))
            try:
                with transaction.atomic():
                    upsert_doc(entity, doc, result)
            except Exception as e:           # one bad document must not stop the run
                log.exception("QBO sync failed for %s %s", entity, doc.get("Id"))
                result.errors.append(f"{entity} {doc.get('DocNumber') or doc.get('Id')}: {e}")
    _close_missing(client, seen, result)
    return result


def _close_missing(client, seen, result):
    """Open QBO orders that didn't come back: tracked elsewhere, GLS removed, or deleted."""
    for order in ShipOrder.objects.filter(source=ShipOrder.Source.QBO,
                                          status__in=OPEN_EDITABLE + (S.POOLED,)
                                          ).exclude(c7_order_id__in=seen):
        entity, txn_id = order.qbo_ref
        try:
            doc = client.read(entity, txn_id)
        except QBOError as e:
            if e.status_code in (400, 404):      # QBO answers a deleted id with a 400 fault
                doc = {}
            else:
                result.errors.append(f"#{order.c7_order_number}: {e}")
                continue
        if doc and is_gls(doc) and not (doc.get("TrackingNum") or "").strip():
            continue                             # just older than the lookback
        why = ("tracking entered in QuickBooks" if doc.get("TrackingNum") else
               "Ship via no longer GLS" if doc else "deleted in QuickBooks")
        if order.status == S.POOLED:
            order.c7_change_note = f"QuickBooks: {why} — pull from pool"
            order.save(update_fields=["c7_change_note", "synced_at"])
            result.flagged.append(order.c7_order_number)
            continue
        order.status = S.FULFILLED if doc.get("TrackingNum") else S.CANCELLED
        order.c7_change_note = f"Closed outside this app ({why})"
        order.save(update_fields=["status", "c7_change_note", "synced_at"])
        result.closed.append(order.c7_order_number)


# --- tracking back to QBO -------------------------------------------------------------

def writeback_on() -> bool:
    return settings.SHIPPING.get("QBO_WRITEBACK", "on") == "on"


def write_tracking(order: ShipOrder, labels, *, client=None, operator=None):
    """Put the tracking number(s) and ship date on the QBO document.

    Same contract as fulfillment.post_order: returns (outcome, message) and
    logs every attempt as a FulfillmentPost. QBO's TrackingNum is one short
    text field, so several boxes are written comma-separated (trimmed to fit).
    QBO sends no email here -- the customer is a business on terms.
    """
    if any(lb.environment != CarrierEnvironment.PRODUCTION for lb in labels):
        return "skipped", "test (sandbox) label -- never written to QuickBooks"
    if not writeback_on():
        return "skipped", "QuickBooks writeback is off (SHIP_QBO_WRITEBACK)"
    tracking = [lb.tracking_number for lb in labels]
    if any(p.succeeded and sorted(p.tracking_numbers) == sorted(tracking)
           for p in order.fulfillment_posts.all()):
        return "skipped", "already written"
    entity, txn_id = order.qbo_ref
    ship_date = timezone.localdate(labels[0].created_at).isoformat()
    try:
        client = client or QBOClient()
        doc = client.read(entity, txn_id)               # fresh SyncToken
        saved = client.sparse_update(entity, txn_id, doc["SyncToken"], {
            "TrackingNum": ", ".join(tracking)[:31], "ShipDate": ship_date})
    except (QBOError, KeyError) as e:
        FulfillmentPost.objects.create(
            order=order, carrier=labels[0].carrier, tracking_numbers=tracking,
            send_email=False, http_status=getattr(e, "status_code", None), succeeded=False,
            operator=operator, response_summary={"target": "qbo", "error": str(e)})
        return "error", str(e)
    FulfillmentPost.objects.create(
        order=order, carrier=labels[0].carrier, tracking_numbers=tracking, send_email=False,
        http_status=200, succeeded=True, operator=operator,
        response_summary={"target": "qbo", "entity": entity, "Id": txn_id,
                          "SyncToken": saved.get("SyncToken")})
    order.status = S.FULFILLED
    order.save(update_fields=["status", "synced_at"])
    return "posted", f"QuickBooks {entity} {order.c7_order_number}: {', '.join(tracking)}"
