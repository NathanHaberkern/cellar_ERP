"""GLS US adapter (formerly GSO): token auth, ship, void.

!! UNVERIFIED !!  GLS US doesn't publish its API; the guide comes with the
account. This is written from GSO's REST web services as they were before the
GLS rebrand, and every endpoint path and field name sits in the constants and
the two build/parse methods below so they can be corrected in one place when
the guide arrives. Nothing else in the app knows GLS's JSON.

    GET    {base}/token      headers UserName / Password / AccountNumber
                             -> token in the "Token" response header
    POST   {base}/Shipment   header Token; {"AccountNumber", "Shipment": {...}}
    DELETE {base}/Shipment   ?TrackingNumber=...&AccountNumber=...

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
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

from .base import (CarrierAdapter, CarrierConfigError, CarrierError, LabelDocumentData,
                   LabelRequest, LabelResult)

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://api.gls-us.com/Rest/v1",       # confirm: GLS may give a separate test URL
    "production": "https://api.gls-us.com/Rest/v1",
}
TIMEOUT_S = 30
TOKEN_TTL_S = 3 * 3600          # GSO tokens lasted a few hours; refreshed on 401 anyway

SERVICE_GROUND = "CPS"          # GLS "California Parcel Service" (ground, western states)
SERVICE_NAMES = {"CPS": "GLS Ground (CPS)"}

SIGNATURE_ADULT = "SIG_REQD_ADULT"
SIGNATURE_REQUIRED = "SIG_REQD"


def default_service(residential: bool | None) -> str:
    return SERVICE_GROUND


class GLSAdapter(CarrierAdapter):
    code = "gls"

    def __init__(self, *, environment, account_number, username, password, base_url="",
                 session=None):
        super().__init__(environment)
        if environment not in BASE_URLS:
            raise CarrierConfigError(f"GLS_ENV must be sandbox or production, not {environment!r}")
        missing = [n for n, v in (("GLS_ACCOUNT_NUMBER", account_number),
                                  ("GLS_USERNAME", username), ("GLS_PASSWORD", password))
                   if not v]
        if missing:
            raise CarrierConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
        self.base_url = (base_url or BASE_URLS[environment]).rstrip("/")
        self.account_number = account_number
        self.username = username
        self.password = password
        self.session = session or requests.Session()

    # --- auth ---------------------------------------------------------------

    def _token_cache_key(self):
        return f"shipping:gls:token:{self.environment}:{self.account_number}"

    def _token(self) -> str:
        key = self._token_cache_key()
        token = cache.get(key)
        if token:
            return token
        resp = self.session.request("GET", f"{self.base_url}/token", timeout=TIMEOUT_S, headers={
            "UserName": self.username, "Password": self.password,
            "AccountNumber": self.account_number})
        token = resp.headers.get("Token") if resp.status_code == 200 else ""
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
                headers={"Token": self._token(), "AccountNumber": self.account_number,
                         "Content-Type": "application/json", "Accept": "application/json"})
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
            "AccountNumber": self.account_number,
            "Shipment": {
                "ServiceCode": req.service,
                "ShipDate": req.ship_date.isoformat(),
                "ShipmentReference": p.reference[:40],
                "SignatureCode": SIGNATURE_ADULT if req.adult_signature else SIGNATURE_REQUIRED,
                "Weight": float(round(Decimal(p.weight_lb), 1)),
                "Length": math.ceil(p.length_in), "Width": math.ceil(p.width_in),
                "Height": math.ceil(p.height_in),
                "ShipperCompany": frm.company[:40], "ShipperContact": frm.name[:40],
                "ShipperPhone": digits(frm.phone), "ShipperEmail": frm.email,
                "PickupAddress1": frm.street1[:40], "PickupAddress2": frm.street2[:40],
                "PickupCity": frm.city, "PickupState": frm.state, "PickupZip": frm.postal_code,
                "ShipToCompany": (to.company or to.name)[:40],
                "ShipToAttention": (to.name or to.company)[:40],
                "ShipToPhone": digits(to.phone),
                "DeliveryAddress1": to.street1[:40], "DeliveryAddress2": to.street2[:40],
                "DeliveryCity": to.city, "DeliveryState": to.state, "DeliveryZip": to.postal_code,
                "DeliveryAddressType": "R" if to.residential else "B",
                "ContainsAlcohol": bool(req.contains_alcohol),
                "LabelType": "PDF_4X6",
            },
        }

    def create_label(self, req: LabelRequest) -> LabelResult:
        body = self._call("POST", "/Shipment", payload=self.build_ship_payload(req))
        return self.parse_ship_response(body, req)

    def parse_ship_response(self, body: dict, req: LabelRequest) -> LabelResult:
        """Defensive on purpose until checked against GLS's guide: finds the
        tracking number and the label image wherever they sit in the response."""
        tracking = _find(body, ("TrackingNumber", "TrackingNo", "trackingNumber"))
        if not tracking:
            raise CarrierError("GLS returned no tracking number", details=_strip_images(body))
        docs = []
        for raw in _find_all(body, ("ShipmentLabel", "Label", "LabelImage", "LabelData",
                                    "label")):
            if isinstance(raw, str) and len(raw) > 100:
                try:
                    docs.append(LabelDocumentData(kind="LABEL",
                                                  content=to_pdf(base64.b64decode(raw))))
                except (ValueError, OSError) as e:
                    log.warning("GLS label image not readable: %s", e)
        if not docs:
            raise CarrierError(f"GLS created {tracking} but returned no readable label -- "
                               f"void it with `void_label`", details=_strip_images(body))
        charge = _find(body, ("TotalCharge", "ShipmentCharge", "NetCharge", "Charge"))
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
        return self._call("DELETE", "/Shipment", params={
            "TrackingNumber": tracking_number, "AccountNumber": self.account_number})


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
