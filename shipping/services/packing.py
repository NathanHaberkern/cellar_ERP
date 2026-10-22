"""Packing rules as pure functions: C7 order -> shipments -> boxes -> pools.

Nothing here touches the database or the network. The live path
(services/pools.py) and the replay check (`shipping_replay`) both feed plain
dataclasses through the same functions, so the exit-gate comparison tests the
exact logic that builds real pools.

The rules are Nate's hand-packing practice (Oct 2026):

Carrier
  * FedEx by default.
  * FedEx-restricted states (settings.SHIPPING["FEDEX_RESTRICTED_STATES"]) -> UPS.
  * A FedEx shipment to a UPS Store -> UPS (FedEx won't deliver there reliably;
    UPS to a UPS Store is fine).

Combining
  * Orders to the exact same address AND the same recipient name share boxes --
    a customer who placed a 4-bottle and a 6-bottle order gets one 10-bottle
    shipment, not two.
  * Same address, different names -> separate shipments, but the same pool.
  * An order still held (requested ship date, address review, compliance) is not
    combined until it is released; it joins the next run.

Boxes (closest fit, fewest packages)
  * Each magnum ships alone in the Magnum box.
  * Each "Port Club Volume" item ships in the Port box. Other port goes in
    ordinary boxes.
  * Everything else: fill 12-bottle boxes, then the smallest box that holds the
    remainder. 42 bottles -> 12 + 12 + 12 + 6.

Pools (~20 twelve-bottle cases of wine = ~240 bottles; 18-22 cases fine)
  * Carrier first: a pool never mixes carriers.
  * The identical default club shipments (exactly 1 each of the default SKUs)
    all go in one Default pool per carrier, whatever its size.
  * The rest are sorted so identical boxes sit together (SKU efficiency), then
    cut into evenly sized pools. All shipments to one address stay in one pool.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

PORT_CLUB_RE = re.compile(r"port\s+club\s+volume", re.I)
UPS_STORE_RE = re.compile(r"\bups\s+store\b|\bthe\s+ups\s+store\b", re.I)
MAGNUM_MIN_ML = 1500

FEDEX = "fedex"
UPS = "ups"


# --- plain data --------------------------------------------------------------

@dataclass
class LineData:
    sku: str
    title: str
    quantity: int
    ml: int | None = None
    bottles_per_unit: int = 1

    @property
    def is_port_club(self) -> bool:
        return bool(PORT_CLUB_RE.search(self.title or ""))

    @property
    def bottles(self) -> int:
        return self.quantity * self.bottles_per_unit


@dataclass
class OrderData:
    """The shipping-relevant slice of one C7 order (or a ShipOrder row)."""
    key: str                         # C7 order id (or DB pk for live runs)
    number: str
    name: str
    street1: str
    city: str
    state: str
    postal_code: str
    street2: str = ""
    company: str = ""
    country: str = "US"
    lines: list[LineData] = field(default_factory=list)
    is_club: bool = False
    club_name: str = ""
    club_package: str = ""
    compliance_status: str = ""
    requested_ship_date: date | None = None
    customer_id: str = ""

    @property
    def bottles(self) -> int:
        return sum(line.bottles for line in self.lines)


@dataclass
class Box:
    """A box size, decoupled from the BoxSize model so tests and replay can build them."""
    name: str
    use: str                         # standard / port_club / magnum
    max_bottles: int
    tare_lb: Decimal
    max_weight_lb: Decimal
    length_in: Decimal = Decimal("0")
    width_in: Decimal = Decimal("0")
    height_in: Decimal = Decimal("0")
    pk: int | None = None


@dataclass
class PlannedPackage:
    box: Box
    contents: list[dict]             # [{"order": number, "sku": sku, "qty": n, "ml": ml}]
    weight_lb: Decimal

    @property
    def bottle_count(self) -> int:
        return sum(c["qty"] for c in self.contents)

    def signature(self):
        return (self.box.name, tuple(sorted((c["sku"], c["qty"]) for c in self.contents)))


@dataclass
class Shipment:
    """One or more orders going to one recipient at one address."""
    carrier: str
    carrier_reason: str
    orders: list[OrderData]
    packages: list[PlannedPackage] = field(default_factory=list)

    @property
    def address_key(self):
        return address_key(self.orders[0])

    @property
    def numbers(self):
        return [o.number for o in self.orders]


@dataclass
class PlannedPool:
    carrier: str
    name: str
    shipments: list[Shipment]
    is_default: bool = False

    @property
    def package_count(self) -> int:
        return sum(len(s.packages) for s in self.shipments)

    @property
    def bottle_count(self) -> int:
        return sum(p.bottle_count for s in self.shipments for p in s.packages)

    @property
    def cases(self) -> float:
        """Twelve-bottle-case equivalents -- how Nate sizes a pool."""
        return round(self.bottle_count / 12, 1)


class PackingError(Exception):
    """An order can't be boxed (unknown bottle size, no box big enough...)."""


# --- C7 normalization ----------------------------------------------------------

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def order_from_c7(o: dict, ml_for_sku=None) -> OrderData:
    """Normalize a raw C7 order. Field names confirmed by c7_probe on our tenant (Oct 2026).

    ml_for_sku(sku, c7_ml) -> (ml, bottles_per_unit) lets the caller apply the
    SkuShippingProfile override and the ERP BottleFormat fallback; without it
    the C7 `volumeInML` is used as-is.
    """
    ship = o.get("shipTo") or {}
    name = " ".join(p for p in (ship.get("firstName"), ship.get("lastName")) if p).strip()
    club = o.get("club") or {}
    lines = []
    for item in o.get("items") or []:
        if item.get("hasShipping") is False:
            continue
        c7_ml = item.get("volumeInML") or None
        ml, per_unit = (ml_for_sku(item.get("sku", ""), c7_ml) if ml_for_sku
                        else (c7_ml, 1))
        lines.append(LineData(sku=item.get("sku") or "", title=item.get("productTitle") or "",
                              quantity=int(item.get("quantity") or 0), ml=ml,
                              bottles_per_unit=per_unit or 1))
    return OrderData(
        key=str(o.get("id")), number=str(o.get("orderNumber", "")), name=name,
        company=ship.get("company") or "", street1=ship.get("address") or "",
        street2=ship.get("address2") or "", city=ship.get("city") or "",
        state=(ship.get("stateCode") or "").upper(), postal_code=ship.get("zipCode") or "",
        country=ship.get("countryCode") or "US", lines=lines,
        is_club=bool(club), club_name=club.get("clubTitle") or "",
        club_package=club.get("clubPackageTitle") or "",
        compliance_status=o.get("complianceStatus") or "",
        requested_ship_date=_parse_date(
            (o.get("selectedShippingOptions") or {}).get("requestedShipDate")),
        customer_id=str(o.get("customerId") or ""),
    )


# --- address matching ----------------------------------------------------------

# USPS-style abbreviations so "123 North Main Street" and "123 N Main St" match.
_ABBREV = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN",
    "COURT": "CT", "BOULEVARD": "BLVD", "PLACE": "PL", "CIRCLE": "CIR", "TERRACE": "TER",
    "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TRAIL": "TRL", "SQUARE": "SQ", "COVE": "CV",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE",
    "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT", "BUILDING": "BLDG", "FLOOR": "FL",
    "#": "#",
}


def norm_text(value: str) -> str:
    tokens = re.sub(r"[^A-Z0-9# ]", " ", (value or "").upper()).split()
    return " ".join(_ABBREV.get(t, t) for t in tokens)


def zip5(value: str) -> str:
    return re.sub(r"\D", "", value or "")[:5]


def address_key(o) -> tuple:
    """Exact-same-address key: street, unit, city, state, ZIP5 after normalization."""
    return (norm_text(o.street1), norm_text(o.street2), norm_text(o.city),
            (o.state or "").upper(), zip5(o.postal_code))


def recipient_key(o) -> tuple:
    return address_key(o) + (norm_text(o.name),)


# --- carrier routing -------------------------------------------------------------

def is_ups_store(o) -> bool:
    return any(UPS_STORE_RE.search(v or "") for v in (o.company, o.street1, o.street2, o.name))


def route(o, restricted_states) -> tuple[str, str]:
    """(carrier, reason). The reason is shown to the packer."""
    state = (o.state or "").upper()
    if state in set(restricted_states):
        return UPS, f"FedEx-restricted state ({state})"
    if is_ups_store(o):
        return UPS, "UPS Store address"
    return FEDEX, ""


# --- box fit ---------------------------------------------------------------------

def _weight(box: Box, bottle_mls, weight_for_ml) -> Decimal:
    total = box.tare_lb + sum((Decimal(weight_for_ml(ml)) for ml in bottle_mls), Decimal("0"))
    return min(total, box.max_weight_lb)


def plan_boxes(orders: list[OrderData], boxes: list[Box], weight_for_ml) -> list[PlannedPackage]:
    """Box one shipment's bottles. Raises PackingError if a size is unknown or nothing fits."""
    standard = sorted([b for b in boxes if b.use == "standard"], key=lambda b: b.max_bottles)
    port = next((b for b in boxes if b.use == "port_club"), None)
    magnum = next((b for b in boxes if b.use == "magnum"), None)
    if not standard:
        raise PackingError("No active standard boxes")
    largest = standard[-1]

    singles = []                     # (box, unit) shipped one per box
    loose = []                       # one entry per bottle, in pack order
    for o in orders:
        for line in o.lines:
            if line.ml is None:
                raise PackingError(f"#{o.number} {line.sku}: bottle size unknown -- set it "
                                   f"under SKU shipping profiles")
            unit = {"order": o.number, "sku": line.sku, "ml": line.ml}
            if line.ml >= MAGNUM_MIN_ML:
                if not magnum:
                    raise PackingError("No active Magnum box")
                singles += [(magnum, unit)] * line.bottles
            elif line.is_port_club:
                if not port:
                    raise PackingError("No active Port box")
                singles += [(port, unit)] * line.bottles
            else:
                loose += [unit] * line.bottles

    packages = []
    for box, unit in singles:
        packages.append(PlannedPackage(box=box, contents=[dict(unit, qty=1)],
                                       weight_lb=_weight(box, [unit["ml"]], weight_for_ml)))

    # Full largest boxes first, then the closest fit for what's left.
    chunks = []
    remaining = list(loose)
    while len(remaining) > largest.max_bottles:
        chunks.append((largest, remaining[:largest.max_bottles]))
        remaining = remaining[largest.max_bottles:]
    if remaining:
        fit = next(b for b in standard if b.max_bottles >= len(remaining))
        chunks.append((fit, remaining))

    for box, units in chunks:
        merged = OrderedDict()
        for u in units:
            k = (u["order"], u["sku"], u["ml"])
            merged[k] = merged.get(k, 0) + 1
        contents = [{"order": k[0], "sku": k[1], "ml": k[2], "qty": n} for k, n in merged.items()]
        packages.append(PlannedPackage(box=box, contents=contents,
                                       weight_lb=_weight(box, [u["ml"] for u in units],
                                                         weight_for_ml)))
    return packages


# --- shipments (combining) -------------------------------------------------------

def build_shipments(orders: list[OrderData], boxes, weight_for_ml, restricted_states):
    """Group orders by recipient+address, route, and box each group.

    Returns (shipments, problems) where problems is [(order_number, message)] for
    orders that couldn't be boxed; those are left out rather than failing the run.
    """
    groups = OrderedDict()
    for o in orders:
        groups.setdefault(recipient_key(o), []).append(o)

    shipments, problems = [], []
    for group in groups.values():
        carrier, reason = route(group[0], restricted_states)
        try:
            packages = plan_boxes(group, boxes, weight_for_ml)
        except PackingError as e:
            problems += [(o.number, str(e)) for o in group]
            continue
        if not packages:
            # e.g. only gift cards / non-shipping lines, or every quantity 0
            problems += [(o.number, "no shippable bottles") for o in group]
            continue
        if len(group) > 1:
            reason = "; ".join(r for r in (reason, f"combined {len(group)} orders") if r)
        shipments.append(Shipment(carrier=carrier, carrier_reason=reason, orders=group,
                                  packages=packages))
    return shipments, problems


# --- pools -------------------------------------------------------------------------

def is_default_shipment(s: Shipment, default_skus) -> bool:
    if len(s.orders) != 1 or not default_skus:
        return False
    lines = s.orders[0].lines
    return (sorted((line.sku, line.quantity) for line in lines)
            == sorted((sku, 1) for sku in default_skus))


def plan_pools(shipments: list[Shipment], *, default_skus, target_bottles=240,
               max_bottles=264, label_prefix="") -> list[PlannedPool]:
    pools = []
    by_carrier = defaultdict(list)
    for s in shipments:
        by_carrier[s.carrier].append(s)

    for carrier in (FEDEX, UPS):
        items = by_carrier.get(carrier) or []
        if not items:
            continue
        cname = "FedEx" if carrier == FEDEX else "UPS"
        defaults = [s for s in items if is_default_shipment(s, default_skus)]
        rest = [s for s in items if s not in defaults]
        if defaults:
            pools.append(PlannedPool(carrier=carrier, name=f"{label_prefix}{cname} Default",
                                     shipments=defaults, is_default=True))

        # Units = all shipments to one address (different names stay together).
        units = OrderedDict()
        for s in rest:
            units.setdefault(s.address_key, []).append(s)

        def sort_key(unit):
            pk = [p for s in unit for p in s.packages]
            biggest = max(p.box.max_bottles for p in pk)
            return (-biggest, sorted(p.signature() for p in pk))

        ordered = sorted(units.values(), key=sort_key)
        def bottles(unit):
            return sum(p.bottle_count for s in unit for p in s.packages)

        total = sum(bottles(u) for u in ordered)
        if not total:
            continue
        n_pools = max(1, math.ceil(total / target_bottles))
        per_pool = total / n_pools          # even split: 540 bottles -> 3 pools of ~180

        current, count, made = [], 0, 0
        for unit in ordered:
            size = bottles(unit)
            if current and (count + size > max_bottles or
                            (count >= per_pool and made < n_pools - 1)):
                made += 1
                pools.append(PlannedPool(carrier=carrier, name=f"{label_prefix}{cname} {made}",
                                         shipments=current))
                current, count = [], 0
            current += unit
            count += size
        if current:
            made += 1
            pools.append(PlannedPool(carrier=carrier, name=f"{label_prefix}{cname} {made}",
                                     shipments=current))
    return pools
