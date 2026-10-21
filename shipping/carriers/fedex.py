"""FedEx REST adapter: OAuth, Ship API create + cancel.

Endpoints (sandbox / production share paths):
    POST /oauth/token                 client_credentials
    POST /ship/v1/shipments           create shipment, labels inline (base64)
    PUT  /ship/v1/shipments/cancel    void

Alcohol: FedEx puts it at PACKAGE level --
    packageSpecialServices.specialServiceTypes += ["ALCOHOL", "SIGNATURE_OPTION"]
    packageSpecialServices.alcoholDetail.alcoholRecipientType = "CONSUMER"
    packageSpecialServices.signatureOptionType = "ADULT"
DTC wine is always CONSUMER, whether the address is a home or an office.

Label images come back in pieceResponses[].packageDocuments[] and, for some
services, transactionShipments[].shipmentDocuments[]. We keep every encoded
document: alcohol shipments can return more than one and all go on the box.
"""
from __future__ import annotations

import base64
import logging
import math
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache

from .base import (CarrierAdapter, CarrierConfigError, CarrierError, LabelDocumentData,
                   LabelRequest, LabelResult)

log = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://apis-sandbox.fedex.com",
    "production": "https://apis.fedex.com",
}
TIMEOUT_S = 30

SERVICE_GROUND = "FEDEX_GROUND"
SERVICE_HOME = "GROUND_HOME_DELIVERY"


def default_service(residential: bool | None) -> str:
    """Home Delivery for residences, Ground for businesses and unknowns.

    Unknown defaults to Ground because FedEx re-rates a Ground package to a
    residence with a surcharge rather than rejecting it; the reverse (Home
    Delivery to a business) is refused. Address validation (Tranche 2) fills
    in `residential` so this fallback is rarely hit.
    """
    return SERVICE_HOME if residential else SERVICE_GROUND


class FedExAdapter(CarrierAdapter):
    code = "fedex"

    def __init__(self, *, environment, client_id, client_secret, account_number,
                 alcohol_shipper_agreement="", session=None):
        super().__init__(environment)
        if environment not in BASE_URLS:
            raise CarrierConfigError(f"FEDEX_ENV must be sandbox or production, not {environment!r}")
        missing = [n for n, v in (("FEDEX_CLIENT_ID", client_id),
                                  ("FEDEX_CLIENT_SECRET", client_secret),
                                  ("FEDEX_ACCOUNT_NUMBER", account_number)) if not v]
        if missing:
            raise CarrierConfigError(f"Missing in .env / config vars: {', '.join(missing)}")
        self.base_url = BASE_URLS[environment]
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_number = account_number
        self.alcohol_shipper_agreement = alcohol_shipper_agreement
        self.session = session or requests.Session()

    # --- auth ---------------------------------------------------------------

    def _token_cache_key(self):
        # Keyed by environment AND client id: switching sandbox -> production
        # keys must never reuse a cached token from the other side.
        return f"shipping:fedex:token:{self.environment}:{self.client_id[-6:]}"

    def _token(self) -> str:
        key = self._token_cache_key()
        token = cache.get(key)
        if token:
            return token
        resp = self.session.post(
            f"{self.base_url}/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT_S,
        )
        body = _json(resp)
        if resp.status_code != 200 or "access_token" not in body:
            code, msg = _first_error(body, default="FedEx authorization failed")
            raise CarrierConfigError(msg, code=code, status=resp.status_code)
        ttl = max(int(body.get("expires_in", 3600)) - 120, 60)
        cache.set(key, body["access_token"], ttl)
        return body["access_token"]

    def _call(self, method, path, payload):
        resp = self.session.request(
            method, f"{self.base_url}{path}", json=payload, timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json",
                     "X-locale": "en_US"},
        )
        body = _json(resp)
        if resp.status_code >= 400 or body.get("errors"):
            code, msg = _first_error(body, default=f"FedEx HTTP {resp.status_code}")
            if resp.status_code == 401:
                cache.delete(self._token_cache_key())
            raise CarrierError(msg, code=code, status=resp.status_code,
                               details=_strip_images(body))
        return body

    # --- ship ---------------------------------------------------------------

    def build_ship_payload(self, req: LabelRequest) -> dict:
        p = req.parcel
        package = {
            "sequenceNumber": 1,
            "weight": {"units": "LB", "value": float(round(Decimal(p.weight_lb), 1))},
            # Whole inches, rounded UP: FedEx truncates decimals when rating, and
            # a box declared smaller than it measures invites a dimension audit.
            "dimensions": {"length": math.ceil(p.length_in), "width": math.ceil(p.width_in),
                           "height": math.ceil(p.height_in), "units": "IN"},
        }
        if p.reference:
            package["customerReferences"] = [
                {"customerReferenceType": "CUSTOMER_REFERENCE", "value": p.reference[:30]}]

        special = {"specialServiceTypes": []}
        if req.contains_alcohol:
            special["specialServiceTypes"].append("ALCOHOL")
            alcohol = {"alcoholRecipientType": "CONSUMER"}
            if self.alcohol_shipper_agreement:
                alcohol["shipperAgreementType"] = self.alcohol_shipper_agreement
            special["alcoholDetail"] = alcohol
        if req.adult_signature:
            special["specialServiceTypes"].append("SIGNATURE_OPTION")
            special["signatureOptionType"] = "ADULT"
        if special["specialServiceTypes"]:
            package["packageSpecialServices"] = special

        return {
            "labelResponseOptions": "LABEL",
            "accountNumber": {"value": self.account_number},
            "requestedShipment": {
                "shipper": _party(req.ship_from),
                "recipients": [_party(req.ship_to)],
                "shipDatestamp": req.ship_date.isoformat(),
                "serviceType": req.service,
                "packagingType": "YOUR_PACKAGING",
                "pickupType": "USE_SCHEDULED_PICKUP",
                "blockInsightVisibility": False,
                "shippingChargesPayment": {"paymentType": "SENDER"},
                "labelSpecification": {"imageType": "PDF", "labelStockType": "STOCK_4X6"},
                "requestedPackageLineItems": [package],
            },
        }

    def create_label(self, req: LabelRequest) -> LabelResult:
        body = self._call("POST", "/ship/v1/shipments", self.build_ship_payload(req))
        return self.parse_ship_response(body, req)

    def parse_ship_response(self, body: dict, req: LabelRequest) -> LabelResult:
        shipments = (body.get("output") or {}).get("transactionShipments") or []
        if not shipments:
            raise CarrierError("FedEx returned no shipment", details=_strip_images(body))
        ship = shipments[0]
        pieces = ship.get("pieceResponses") or []
        tracking = (pieces[0].get("trackingNumber") if pieces else "") or \
            ship.get("masterTrackingNumber", "")
        if not tracking:
            raise CarrierError("FedEx returned no tracking number", details=_strip_images(body))

        docs = []
        for piece in pieces:
            docs += _documents(piece.get("packageDocuments"))
        docs += _documents(ship.get("shipmentDocuments"))
        if not docs:
            raise CarrierError(
                f"FedEx created {tracking} but returned no label image -- void it in "
                f"FedEx Ship Manager or with `void_label`", details=_strip_images(body))

        charge, currency = _net_charge(ship, pieces)
        return LabelResult(
            carrier=self.code, environment=self.environment,
            service=ship.get("serviceType") or req.service,
            tracking_number=tracking,
            master_tracking_number=ship.get("masterTrackingNumber", ""),
            net_charge=charge, currency=currency, documents=docs,
            response_summary=_strip_images(body),
        )

    # --- void ---------------------------------------------------------------

    def void_label(self, tracking_number: str) -> dict:
        body = self._call("PUT", "/ship/v1/shipments/cancel", {
            "accountNumber": {"value": self.account_number},
            "trackingNumber": tracking_number,
            "deletionControl": "DELETE_ALL_PACKAGES",
        })
        if not (body.get("output") or {}).get("cancelledShipment"):
            raise CarrierError(f"FedEx did not cancel {tracking_number}", details=body)
        return body


# --- helpers ----------------------------------------------------------------

def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"errors": [{"code": f"HTTP_{resp.status_code}", "message": resp.text[:300]}]}


def _first_error(body, default):
    errors = body.get("errors") or []
    if errors:
        e = errors[0]
        return e.get("code", ""), e.get("message") or default
    return "", body.get("error_description") or default


def _party(a):
    contact = {"phoneNumber": "".join(ch for ch in a.phone if ch.isdigit())}
    if a.name:
        contact["personName"] = a.name
    if a.company:
        contact["companyName"] = a.company
    if a.email:
        contact["emailAddress"] = a.email
    address = {
        "streetLines": [s for s in (a.street1, a.street2) if s],
        "city": a.city, "stateOrProvinceCode": a.state,
        "postalCode": a.postal_code, "countryCode": a.country,
    }
    if a.residential is not None:
        address["residential"] = a.residential
    return {"contact": contact, "address": address}


def _documents(items):
    out = []
    for d in items or []:
        encoded = d.get("encodedLabel")
        if not encoded:
            continue
        kind = d.get("contentType") or d.get("docType") or "LABEL"
        out.append(LabelDocumentData(kind=kind, content=base64.b64decode(encoded),
                                     content_type="application/pdf"))
    return out


def _net_charge(ship, pieces):
    try:
        rates = ship["completedShipmentDetail"]["shipmentRating"]["shipmentRateDetails"]
        if rates:
            amt = rates[0].get("totalNetCharge")
            if amt is not None:
                return Decimal(str(amt)), rates[0].get("currency", "USD")
    except (KeyError, TypeError, InvalidOperation):
        pass
    for piece in pieces:
        amt = piece.get("netChargeAmount")
        if amt is not None:
            try:
                return Decimal(str(amt)), piece.get("currency", "USD")
            except InvalidOperation:
                pass
    return None, ""


def _strip_images(obj):
    """Copy of a response with base64 label images replaced by their size.

    Responses are kept for audit; the images themselves live in LabelDocument,
    so storing them twice would only bloat the JSON column.
    """
    if isinstance(obj, dict):
        return {k: (f"<{len(v)} chars stripped>" if k == "encodedLabel" and isinstance(v, str)
                    else _strip_images(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_images(v) for v in obj]
    return obj
