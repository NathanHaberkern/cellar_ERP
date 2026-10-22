"""UPS REST adapter: OAuth, Shipping API create + void.

Endpoints (CIE = UPS's test environment; paths are the same in production):
    POST /security/v1/oauth/token                       client_credentials, HTTP Basic
    POST /api/shipments/v2409/ship                      create shipment, label inline (base64)
    DELETE /api/shipments/v2409/void/cancel/{shipment}  void

Written from UPS's published OpenAPI spec (github.com/UPS-API/api-documentation,
Shipping.yaml, v2409). Not yet run against CIE -- the first `ups_test_label`
is the check, same as FedEx in Tranche 1.

Alcohol: UPS puts it at SHIPMENT level --
    ShipmentServiceOptions.RestrictedArticles.AlcoholicBeveragesIndicator = ""
(a presence flag; the account needs UPS's alcohol contract or the call is refused).
Adult signature is at PACKAGE level: PackageServiceOptions.DeliveryConfirmation.DCISType = "3".

Labels: UPS's image formats are GIF/ZPL/EPL/SPL -- no PDF. We ask for GIF and
convert it to a 4x6 PDF here, so everything above this file (storage, the
print page, merged pool PDFs) only ever sees PDF, same as FedEx.
"""
from __future__ import annotations

import base64
import io
import logging
import math
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

from .base import (CarrierAdapter, CarrierConfigError, CarrierError, LabelDocumentData,
                   LabelRequest, LabelResult)

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://wwwcie.ups.com",
    "production": "https://onlinetools.ups.com",
}
API_VERSION = "v2409"
TIMEOUT_S = 30

SERVICE_GROUND = "03"
SERVICE_NAMES = {"03": "UPS Ground"}

# UPS's GIF label is landscape (about 1400 x 800 px): the 6 x 4 in label plus a
# ~1 in tab on the right edge. We crop the tab and rotate to portrait 4 x 6.
# Verify orientation on the first CIE label; flip GIF_ROTATE to 90 if upside down.
GIF_LABEL_ASPECT = Decimal("1.5")
GIF_ROTATE = -90


def default_service(residential: bool | None) -> str:
    """UPS Ground for everything. UPS has no separate residential service --
    the residential indicator on the address sets the surcharge instead."""
    return SERVICE_GROUND


class UPSAdapter(CarrierAdapter):
    code = "ups"

    def __init__(self, *, environment, client_id, client_secret, account_number, session=None):
        super().__init__(environment)
        if environment not in BASE_URLS:
            raise CarrierConfigError(f"UPS_ENV must be sandbox or production, not {environment!r}")
        missing = [n for n, v in (("UPS_CLIENT_ID", client_id),
                                  ("UPS_CLIENT_SECRET", client_secret),
                                  ("UPS_ACCOUNT_NUMBER", account_number)) if not v]
        if missing:
            raise CarrierConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
        self.base_url = BASE_URLS[environment]
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_number = account_number
        self.session = session or requests.Session()

    # --- auth ---------------------------------------------------------------

    def _token_cache_key(self):
        return f"shipping:ups:token:{self.environment}:{self.client_id[-6:]}"

    def _token(self) -> str:
        key = self._token_cache_key()
        token = cache.get(key)
        if token:
            return token
        resp = self.session.post(
            f"{self.base_url}/security/v1/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "x-merchant-id": self.account_number},
            timeout=TIMEOUT_S,
        )
        body = _json(resp)
        if resp.status_code != 200 or "access_token" not in body:
            code, msg = _first_error(body, default="UPS authorization failed")
            raise CarrierConfigError(msg, code=code, status=resp.status_code)
        try:
            ttl = max(int(body.get("expires_in", 3600)) - 120, 60)
        except (TypeError, ValueError):
            ttl = 3000
        cache.set(key, body["access_token"], ttl)
        return body["access_token"]

    def _call(self, method, path, payload=None):
        resp = self.session.request(
            method, f"{self.base_url}{path}", json=payload, timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json",
                     "transId": uuid.uuid4().hex,          # max 32 chars
                     "transactionSrc": "cellar_erp"},
        )
        body = _json(resp)
        if resp.status_code >= 400 or (body.get("response") or {}).get("errors"):
            code, msg = _first_error(body, default=f"UPS HTTP {resp.status_code}")
            if resp.status_code == 401:
                cache.delete(self._token_cache_key())
            raise CarrierError(msg, code=code, status=resp.status_code,
                               details=_strip_images(body))
        return body

    # --- ship ---------------------------------------------------------------

    def build_ship_payload(self, req: LabelRequest) -> dict:
        p = req.parcel
        package = {
            "Packaging": {"Code": "02", "Description": "Customer Supplied Package"},
            # Whole inches, rounded up -- same rule as FedEx (see fedex.py).
            "Dimensions": {"UnitOfMeasurement": {"Code": "IN"},
                           "Length": str(math.ceil(p.length_in)),
                           "Width": str(math.ceil(p.width_in)),
                           "Height": str(math.ceil(p.height_in))},
            "PackageWeight": {"UnitOfMeasurement": {"Code": "LBS"},
                              "Weight": str(round(Decimal(p.weight_lb), 1))},
        }
        if p.reference:
            package["ReferenceNumber"] = [{"Value": p.reference[:35]}]
        if req.adult_signature:
            package["PackageServiceOptions"] = {"DeliveryConfirmation": {"DCISType": "3"}}

        shipper = _party(req.ship_from)
        shipper["ShipperNumber"] = self.account_number
        shipment = {
            "Description": "Wine",
            "Shipper": shipper,
            "ShipFrom": _party(req.ship_from),
            "ShipTo": _party(req.ship_to),
            "PaymentInformation": {"ShipmentCharge": [
                {"Type": "01", "BillShipper": {"AccountNumber": self.account_number}}]},
            "Service": {"Code": req.service, "Description": SERVICE_NAMES.get(req.service, "")},
            "ShipmentRatingOptions": {"NegotiatedRatesIndicator": ""},
            "Package": [package],
        }
        if req.contains_alcohol:
            shipment["ShipmentServiceOptions"] = {
                "RestrictedArticles": {"AlcoholicBeveragesIndicator": ""}}
        return {"ShipmentRequest": {
            "Request": {"RequestOption": "nonvalidate",
                        "TransactionReference": {"CustomerContext": p.reference[:512]}},
            "Shipment": shipment,
            "LabelSpecification": {"LabelImageFormat": {"Code": "GIF"},
                                   "HTTPUserAgent": "Mozilla/4.5",
                                   "LabelStockSize": {"Height": "6", "Width": "4"}},
        }}

    def create_label(self, req: LabelRequest) -> LabelResult:
        body = self._call("POST", f"/api/shipments/{API_VERSION}/ship",
                          self.build_ship_payload(req))
        return self.parse_ship_response(body, req)

    def parse_ship_response(self, body: dict, req: LabelRequest) -> LabelResult:
        results = (body.get("ShipmentResponse") or {}).get("ShipmentResults") or {}
        pieces = results.get("PackageResults") or []
        if isinstance(pieces, dict):          # one package may come back as an object
            pieces = [pieces]
        shipment_id = results.get("ShipmentIdentificationNumber", "")
        tracking = (pieces[0].get("TrackingNumber") if pieces else "") or shipment_id
        if not tracking:
            raise CarrierError("UPS returned no tracking number", details=_strip_images(body))

        docs = []
        for piece in pieces:
            image = (piece.get("ShippingLabel") or {}).get("GraphicImage")
            if image:
                docs.append(LabelDocumentData(kind="LABEL", content=gif_to_pdf(
                    base64.b64decode(image)), content_type="application/pdf"))
        if not docs:
            raise CarrierError(
                f"UPS created {tracking} but returned no label image -- void it on ups.com "
                f"or with `void_label`", details=_strip_images(body))

        charge, currency = _net_charge(results)
        return LabelResult(
            carrier=self.code, environment=self.environment, service=req.service,
            tracking_number=tracking, master_tracking_number=shipment_id,
            net_charge=charge, currency=currency, documents=docs,
            response_summary=_strip_images(body),
        )

    # --- void ---------------------------------------------------------------

    def void_label(self, tracking_number: str) -> dict:
        # A one-package shipment's identification number is its tracking number.
        body = self._call("DELETE", f"/api/shipments/{API_VERSION}/void/cancel/{tracking_number}")
        status = (((body.get("VoidShipmentResponse") or {}).get("SummaryResult") or {})
                  .get("Status") or {})
        if str(status.get("Code")) != "1":
            raise CarrierError(f"UPS did not void {tracking_number}", details=body)
        return body


# --- label image -------------------------------------------------------------

def gif_to_pdf(gif: bytes) -> bytes:
    """UPS GIF label -> one 4 x 6 in PDF page, portrait, ready to print."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.open(io.BytesIO(gif))
    img.load()
    img = img.convert("RGB")
    w, h = img.size
    if w > h:
        label_w = int(h * GIF_LABEL_ASPECT)          # 6 x 4 label area, drop the tab
        if label_w < w:
            img = img.crop((0, 0, label_w, h))
        img = img.rotate(GIF_ROTATE, expand=True)

    page_w, page_h = 4 * 72, 6 * 72
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(page_w, page_h))
    iw, ih = img.size
    scale = min(page_w / iw, page_h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(ImageReader(img), (page_w - dw) / 2, page_h - dh, dw, dh)
    c.showPage()
    c.save()
    return out.getvalue()


# --- helpers ----------------------------------------------------------------

def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"response": {"errors": [{"code": f"HTTP_{resp.status_code}",
                                         "message": resp.text[:300]}]}}


def _first_error(body, default):
    errors = (body.get("response") or {}).get("errors") or []
    if errors:
        e = errors[0]
        return e.get("code", ""), e.get("message") or default
    return "", body.get("error_description") or default


def _party(a):
    """UPS name/attention/address lines are capped at 35 characters."""
    name = (a.company or a.name)[:35]
    party = {
        "Name": name,
        "AttentionName": (a.name or a.company)[:35],
        "Phone": {"Number": "".join(ch for ch in a.phone if ch.isdigit())[:15]},
        "Address": {
            "AddressLine": [s[:35] for s in (a.street1, a.street2) if s],
            "City": a.city[:30], "StateProvinceCode": a.state,
            "PostalCode": a.postal_code, "CountryCode": a.country or "US",
        },
    }
    if a.residential:
        party["Address"]["ResidentialAddressIndicator"] = ""
    return party


def _money(container):
    try:
        amt = (container or {}).get("MonetaryValue")
        if amt not in (None, ""):
            return Decimal(str(amt)), container.get("CurrencyCode", "USD")
    except (InvalidOperation, AttributeError):
        pass
    return None, ""


def _net_charge(results):
    """Negotiated (our contract) rate when UPS returns one, else published."""
    charge, currency = _money((results.get("NegotiatedRateCharges") or {}).get("TotalCharge"))
    if charge is None:
        charge, currency = _money((results.get("ShipmentCharges") or {}).get("TotalCharges"))
    return charge, currency


def _strip_images(obj):
    if isinstance(obj, dict):
        return {k: (f"<{len(v)} chars stripped>"
                    if k in ("GraphicImage", "HTMLImage") and isinstance(v, str)
                    else _strip_images(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_images(v) for v in obj]
    return obj
