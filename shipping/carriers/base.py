"""Carrier-neutral types and the adapter interface.

Everything above this layer (services, views, commands) speaks only these
dataclasses. FedEx and UPS each get one adapter that translates to and from
their own JSON. That keeps carrier quirks -- FedEx's two alcohol documents,
UPS's shipper-number format -- in one file each, and lets tests swap in a fake.
"""
from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal


class CarrierError(Exception):
    """A carrier refused or failed a request. `code` is the carrier's own code."""

    def __init__(self, message, *, code="", status=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def __str__(self):
        prefix = f"[{self.code}] " if self.code else ""
        return f"{prefix}{self.args[0]}"


class CarrierConfigError(CarrierError):
    """Missing or invalid credentials/settings -- fix .env, not the request."""


@dataclass
class Address:
    name: str
    street1: str
    city: str
    state: str
    postal_code: str
    country: str = "US"
    company: str = ""
    street2: str = ""
    phone: str = ""
    email: str = ""
    residential: bool | None = None

    @classmethod
    def from_settings(cls, data: dict) -> "Address":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Parcel:
    length_in: Decimal
    width_in: Decimal
    height_in: Decimal
    weight_lb: Decimal
    reference: str = ""          # printed on the label (C7 order number)


@dataclass
class LabelRequest:
    ship_from: Address
    ship_to: Address
    parcel: Parcel
    service: str
    ship_date: dt.date
    contains_alcohol: bool = True
    adult_signature: bool = True


@dataclass
class LabelDocumentData:
    kind: str
    content: bytes
    content_type: str = "application/pdf"


@dataclass
class LabelResult:
    carrier: str
    environment: str
    service: str
    tracking_number: str
    master_tracking_number: str = ""
    net_charge: Decimal | None = None
    currency: str = ""
    documents: list[LabelDocumentData] = field(default_factory=list)
    response_summary: dict = field(default_factory=dict)


@dataclass
class AddressValidation:
    """Carrier-neutral result of an address check.

    `resolved` -- the carrier matched it to a real delivery point.
    `suggested` -- the carrier's standardized version of the address.
    `classification` -- RESIDENTIAL / BUSINESS / MIXED / UNKNOWN.
    `issues` -- human-readable problems (suite missing, interpolated, ...).
    """
    resolved: bool
    suggested: Address | None
    classification: str = "UNKNOWN"
    issues: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def residential(self) -> bool | None:
        return {"RESIDENTIAL": True, "BUSINESS": False}.get(self.classification)


class CarrierAdapter(abc.ABC):
    code: str = ""

    def __init__(self, environment: str):
        self.environment = environment

    @abc.abstractmethod
    def create_label(self, request: LabelRequest) -> LabelResult:
        """Buy one label. Raises CarrierError; never returns a partial result."""

    @abc.abstractmethod
    def void_label(self, tracking_number: str) -> dict:
        """Cancel a label at the carrier. Returns the carrier's response summary."""

    def validate_address(self, address: Address) -> AddressValidation:
        """Optional: carriers without an address API raise CarrierConfigError."""
        raise CarrierConfigError(f"{self.code} has no address validation")
