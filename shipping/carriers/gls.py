"""GLS US adapter: token auth, ship, void, on-call pickup.

Written to GLS's ShipmentHub API v2 (https://shipmenthub.gls-us.com, a Postman
collection, read 2026-10-22). Not yet run against GLS with real credentials.

    GET    {base}/Token      headers Username / Password -> "Token" (+ "TokenExpiry")
                             response headers; good for 12 hours
    POST   {base}/Shipment   header Token; {"AccountNumber", "Shipment": {...}}
                             -> TrackingNumber, ShipmentCharges.TotalCharge, PaperLabel
                             (base64 PDF when ShipmentLabelType = PDF_4x5LABEL)
    DELETE {base}/Shipment   body {"AccountNumber", "TrackingNumber"}
    POST   {base}/Pickup     on-call pickup -> a confirmation number

Every call sends `Accept/Content-Type: ...; ver=2.0`, as the collection does.
Errors come back as {"StatusCode", "ErrorCount", "ErrorDetail": [{"ErrorCode",
"ErrorDescription"}]}.

GLS has NO test server: GLS_ENV=sandbox still calls the live API. That's safe
for labels because GLS only bills a shipment once it is scanned at a GLS
facility (unscanned data is dropped after 90 days) -- void test labels anyway.
Pickups are real dispatches, so request_pickup refuses unless GLS_ENV=production.

What GLS is used for here: wholesale cases to licensed accounts, queued from
QuickBooks invoices whose Ship via is GLS (see services/qbo_intake.py).
Signature: adult signature when the request asks for it, else signature
required -- wine never ships without one.
"""
from __future__ import annotations

import base64
import io
import logging
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

from .base import (CarrierAdapter, CarrierConfigError, CarrierError, LabelDocumentData,
                   LabelRequest, LabelResult)

log = logging.getLogger(__name__)

BASE_URL = "https://ship.gls-us.com/v2"
TIMEOUT_S = 30
TOKEN_TTL_S = 11 * 3600          # GLS tokens last 12 h; refreshed on 401 anyway
ACCEPT = "text/plain; ver=2.0"
CONTENT_TYPE = "application/json-patch+json; ver=2.0"

SERVICE_GROUND = "CPS"           # GLS Ground
SERVICE_NAMES = {"CPS": "GLS Ground", "PDS": "GLS Priority Overnight",
                 "EPS": "GLS Early Priority Overnight", "NPS": "GLS Noon Priority Overnight",
                 "SDS": "GLS Saturday Delivery"}

SIGNATURE_ADULT = "ADULT_SIG_REQD"
SIGNATURE_REQUIRED = "SIG_REQD"
LABEL_TYPE = "PDF_4x5LABEL"      # GLS's only PDF label: 4 x 5, prints fine on 4 x 6 stock


def default_service(residential: bool | None) -> str:
    return SERVICE_GROUND


@dataclass
class PickupRequest:
    pickup_date: object                  # date
    earliest: str                        # "12:00 pm"
    latest: str                          # "05:00 pm"
    packages: int
    total_weight_lb: Decimal
    company: str
    contact: str
    phone: str
    email: str
    street1: str
    street2: str
    city: str
    state: str
    postal_code: str
    instructions: str = ""


class GLSAdapter(CarrierAdapter):
    code = "gls"

    def __init__(self, *, environment, account_number, username, password, base_url="",
                 session=None):
        super().__init__(environment)
        if environment not in ("sandbox", "production"):
            raise CarrierConfigError(f"GLS_ENV must be sandbox or production, not {environment!r}")
        missing = [n for n, v in (("GLS_ACCOUNT_NUMBER", account_number),
                                  ("GLS_USERNAME", username), ("GLS_PASSWORD", password))
                   if not v]
        if missing:
            raise CarrierConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.account_number = str(account_number).strip()
        self.username = username
        self.password = password
        self.session = session or requests.Session()

    @property
    def _account(self):
        # Documented as Integer(10); the collection's own example sends a string.
        return int(self.account_number) if self.account_number.isdigit() else self.account_number

    # --- auth ---------------------------------------------------------------

    def _token_cache_key(self):
        return f"shipping:gls:token:{self.account_number}:{self.username}"

    def _token(self) -> str:
        key = self._token_cache_key()
        token = cache.get(key)
        if token:
            return token
        resp = self.session.request("GET", f"{self.base_url}/Token", timeout=TIMEOUT_S, headers={
            "Accept": ACCEPT, "Username": self.username, "Password": self.password})
        token = (resp.headers.get("Token") or "") if resp.status_code in (200, 201) else ""
        if not token:
            code, msg = _first_error(_json(resp), default=f"GLS authorization failed "
                                                          f"(HTTP {resp.status_code})")
            raise CarrierConfigError(msg, code=code, status=resp.status_code)
        cache.set(key, token, TOKEN_TTL_S)
        return token

    def _call(self, method, path, *, payload=None, params=None):
        for attempt in (1, 2):
            resp = self.session.request(
                method, f"{self.base_url}{path}", json=payload, params=params,
                timeout=TIMEOUT_S,
                headers={"Token": self._token(), "Content-Type": CONTENT_TYPE,
                         "Accept": ACCEPT})
            if resp.status_code == 401 and attempt == 1:
                cache.delete(self._token_cache_key())
                continue
            break
        body = _json(resp)
        if resp.status_code >= 400 or _first_error(body, default="")[1]:
            code, msg = _first_error(body, default=f"GLS HTTP {resp.status_code}")
            raise CarrierError(msg, code=code, status=resp.status_code,
                               details=_strip_images(body))
        return body

    # --- ship ---------------------------------------------------------------

    def build_ship_payload(self, req: LabelRequest) -> dict:
        p, frm, to = req.parcel, req.ship_from, req.ship_to
        digits = lambda v: "".join(ch for ch in (v or "") if ch.isdigit())  # noqa: E731
        return {
            "AccountNumber": self._account,
            "Shipment": {
                "ShipperCompany": frm.company[:50], "ShipperContact": frm.name[:50],
                "ShipperPhone": digits(frm.phone)[:20], "ShipperEmail": frm.email[:50],
                "PickupAddress1": frm.street1[:50], "PickupAddress2": frm.street2[:50],
                "PickupCity": frm.city[:50], "PickupState": frm.state,
                "PickupZip": frm.postal_code, "PickupCountryCode": "US",
                "PickupAddressType": "B",
                "ShipToCompany": (to.company or to.name)[:50],
                "ShipToAttention": (to.name or to.company)[:50],
                "ShipToPhone": digits(to.phone)[:20],
                "DeliveryAddress1": to.street1[:50], "DeliveryAddress2": to.street2[:50],
                "DeliveryCity": to.city[:50], "DeliveryState": to.state,
                "DeliveryZip": to.postal_code, "DeliveryCountryCode": to.country or "US",
                "DeliveryAddressType": "R" if to.residential else "B",
                "ShipDate": req.ship_date.isoformat(),
                "ServiceCode": req.service,
                # "Package weight in pounds. Round up to next pound."
                "Weight": math.ceil(Decimal(p.weight_lb)), "WeightUnitOfMeasure": "lb",
                "Dimensions": {"Length": math.ceil(p.length_in), "Width": math.ceil(p.width_in),
                               "Height": math.ceil(p.height_in), "UnitOfMeasure": "inch"},
                "ShipmentLabelType": LABEL_TYPE,
                "SignatureCode": SIGNATURE_ADULT if req.adult_signature else SIGNATURE_REQUIRED,
                "ShipmentReference": p.reference[:25],
                "CommercialInvoiceAttached": False,
            },
        }

    def create_label(self, req: LabelRequest) -> LabelResult:
        body = self._call("POST", "/Shipment", payload=self.build_ship_payload(req))
        return self.parse_ship_response(body, req)

    def parse_ship_response(self, body: dict, req: LabelRequest) -> LabelResult:
        tracking = body.get("TrackingNumber")
        if not tracking:
            raise CarrierError("GLS returned no tracking number", details=_strip_images(body))
        docs = []
        raw = body.get("PaperLabel")
        if isinstance(raw, str) and raw:
            try:
                docs.append(LabelDocumentData(kind="LABEL",
                                              content=to_pdf(base64.b64decode(raw))))
            except (ValueError, OSError) as e:
                log.warning("GLS label not readable: %s", e)
        if not docs:
            raise CarrierError(f"GLS created {tracking} but returned no readable label -- "
                               f"void it with `void_label`", details=_strip_images(body))
        charge = (body.get("ShipmentCharges") or {}).get("TotalCharge")
        try:
            charge = Decimal(str(charge)) if charge not in (None, "") else None
        except InvalidOperation:
            charge = None
        return LabelResult(carrier=self.code, environment=self.environment,
                           service=req.service, tracking_number=str(tracking),
                           net_charge=charge, currency="USD" if charge is not None else "",
                           documents=docs, response_summary=_strip_images(body))

    # --- void ---------------------------------------------------------------

    def void_label(self, tracking_number: str) -> dict:
        return self._call("DELETE", "/Shipment", payload={
            "AccountNumber": self._account, "TrackingNumber": tracking_number})

    # --- pickup -------------------------------------------------------------

    def request_pickup(self, pr: PickupRequest) -> tuple[str, dict]:
        """Book an on-call pickup. Returns (confirmation number, response)."""
        if self.environment != "production":
            raise CarrierConfigError("GLS has no test server, so pickups are only requested "
                                     "with GLS_ENV=production.")
        digits = "".join(ch for ch in (pr.phone or "") if ch.isdigit())
        body = self._call("POST", "/Pickup", payload={
            "AccountNumber": self._account,
            "PickupDate": pr.pickup_date.isoformat(),
            "EarliestPickupTime": pr.earliest, "LatestPickupTime": pr.latest,
            "NumberofPackages": pr.packages,
            "TotalWeight": math.ceil(Decimal(pr.total_weight_lb)),
            "OnCallPickupLocation": {
                "PickupCompany": pr.company[:50], "PickupAddress1": pr.street1[:50],
                "PickupAddress2": pr.street2[:50], "PickupCity": pr.city[:50],
                "PickupState": pr.state, "PickupZip": pr.postal_code,
                "PickupAddressType": "B", "PickupContact": pr.contact[:50],
                "PickupContactEmail": pr.email[:50], "PickupContactPhone": digits[:20],
                "PickupRequestNotification": bool(pr.email),
                "PickupInstruction": pr.instructions[:100],
            },
        })
        # The collection's response table is cut off before the field name.
        confirmation = _find(body, ("PickupConfirmationNumber", "ConfirmationNumber",
                                    "PickupConfirmation", "PickupNumber",
                                    "ConfirmationNo"))
        if not confirmation:
            raise CarrierError("GLS accepted the pickup but returned no confirmation number "
                               "-- check with GLS before requesting again",
                               details=_strip_images(body))
        return str(confirmation), _strip_images(body)


# --- helpers ----------------------------------------------------------------

def to_pdf(data: bytes) -> bytes:
    """A label as one 4 x 6 PDF page: PDFs pass through, images are placed on a page."""
    if data[:4] == b"%PDF":
        return data
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGB")
    if img.width > img.height:
        img = img.rotate(-90, expand=True)
    page_w, page_h = 4 * 72, 6 * 72
    scale = min(page_w / img.width, page_h / img.height)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img), (page_w - img.width * scale) / 2,
                page_h - img.height * scale, img.width * scale, img.height * scale)
    c.showPage()
    c.save()
    return out.getvalue()


def _json(resp):
    try:
        return resp.json() if resp.text else {}
    except ValueError:
        return {"ErrorMessage": resp.text[:300]}


def _first_error(body, default):
    if not isinstance(body, dict):
        return "", default
    detail = body.get("ErrorDetail")
    if isinstance(detail, dict):
        detail = [detail]
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        e = detail[0]
        msg = e.get("ErrorDescription") or e.get("ErrorMessage")
        if msg:
            return str(e.get("ErrorCode") or e.get("ErrorId") or ""), msg
    for key in ("ErrorMessage", "Error", "Message", "error"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return str(body.get("ErrorCode") or ""), v
    errs = body.get("Errors") or body.get("errors")
    if isinstance(errs, list) and errs:
        e = errs[0] if isinstance(errs[0], dict) else {"Message": str(errs[0])}
        return str(e.get("Code") or e.get("ErrorCode") or ""), \
            e.get("Message") or e.get("ErrorMessage") or default
    return "", default


def _find_all(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and not isinstance(v, (dict, list)):
                yield v
            else:
                yield from _find_all(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_all(v, keys)


def _find(obj, keys):
    return next(iter(_find_all(obj, keys)), None)


def _strip_images(obj):
    if isinstance(obj, dict):
        return {k: (f"<{len(v)} chars stripped>" if isinstance(v, str) and len(v) > 500
                    else _strip_images(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_images(v) for v in obj]
    return obj
