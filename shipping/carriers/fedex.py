"""FedEx REST adapter: OAuth, Ship API create + cancel.

Endpoints (sandbox / production share paths):
    POST /oauth/token                 client_credentials
    POST /ship/v1/shipments           create shipment, labels inline (base64)
    PUT  /ship/v1/shipments/cancel    void
    POST /address/v1/addresses/resolve  address validation + residential/business

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

from .base import (Address, AddressValidation, CarrierAdapter, CarrierConfigError,
                   CarrierError, LabelDocumentData, LabelRequest, LabelResult)

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


    # --- address validation -------------------------------------------------

    def validate_address(self, address: Address) -> AddressValidation:
        body = self._call("POST", "/address/v1/addresses/resolve", {
            "addressesToValidate": [{"address": {
                "streetLines": [s for s in (address.street1, address.street2) if s],
                "city": address.city, "stateOrProvinceCode": address.state,
                "postalCode": address.postal_code, "countryCode": address.country or "US",
            }}]})
        return parse_address_response(body, address)


def _squash(v):
    return "".join(ch for ch in (v or "").upper() if ch.isalnum())


def _truthy(v):
    return v is True or str(v).strip().lower() == "true"


# FedEx attribute -> problem shown to the reviewer when it's true.
_BAD_ATTRS = {
    "SuiteRequiredButMissing": "Suite/apt number required but missing",
    "InvalidSuiteNumber": "Suite/apt number not valid for this building",
    "InterpolatedStreetAddress": "Street number estimated, not confirmed",
    "MultipleMatches": "Matches more than one address",
    "POBox": "PO Box (carriers can't deliver wine there)",
}


def parse_address_response(body: dict, original: Address) -> AddressValidation:
    """Turn FedEx's resolve response into an AddressValidation.

    Defensive by design: FedEx returns attribute values as "true"/"false"
    strings, sometimes omits parsedPostalCode, and the sandbox returns canned
    data. Anything missing is treated as "not confirmed", which sends the order
    to review rather than letting a bad address through.
    """
    resolved_list = (body.get("output") or {}).get("resolvedAddresses") or []
    if not resolved_list:
        return AddressValidation(resolved=False, suggested=None,
                                 issues=["FedEx returned no match"], raw=body)
    r = resolved_list[0]
    attrs = r.get("attributes") or {}
    issues = [msg for key, msg in _BAD_ATTRS.items() if _truthy(attrs.get(key))]
    resolved = _truthy(attrs.get("Resolved", "false"))
    if not resolved:
        issues.insert(0, "FedEx could not resolve this address")
    elif "DPV" in attrs and not _truthy(attrs.get("DPV")):
        issues.insert(0, "Not a confirmed delivery point")
    for m in r.get("customerMessages") or []:
        code = m.get("code") or m.get("message")
        if code and code not in issues and "STANDARDIZED" not in str(code).upper():
            issues.append(str(code))

    parsed = r.get("parsedPostalCode") or {}
    postal = parsed.get("base") or r.get("postalCode") or original.postal_code
    lines = r.get("streetLinesToken") or r.get("streetLines") or []
    if len(lines) > 1:
        street2 = " ".join(lines[1:])
    elif lines and original.street2 and _squash(original.street2) in _squash(lines[0]):
        street2 = ""             # FedEx folded the apt/suite into line 1
    else:
        street2 = original.street2
    suggested = Address(
        name=original.name, company=original.company, phone=original.phone,
        email=original.email,
        street1=(lines[0] if lines else original.street1),
        street2=street2,
        city=r.get("city") or original.city,
        state=r.get("stateOrProvinceCode") or original.state,
        postal_code=postal, country=r.get("countryCode") or original.country,
    )
    return AddressValidation(resolved=resolved, suggested=suggested,
                             classification=(r.get("classification") or "UNKNOWN").upper(),
                             issues=issues, raw=body)


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
