"""Turn one parsed member request into the exact changes we propose.

plan() is pure: it takes the request plus facts already read from Commerce7 and
shipping (see Facts) and returns a Plan. Nothing here calls the network, so every
rule is unit-tested and the approval page shows exactly what apply() will send.

Stage matters. Before the release is built, a change goes on the membership and
the next build picks it up. After the build, the member already has an order
(usually paid), and C7's API cannot edit an order in place (PUT /order/upsert
deletes and recreates it, which we never do), so order-side changes are either
a hold/ship date on our own ShipOrder or a manual step in C7 admin.

Action shapes:
  {"target": "c7", "method", "path", "body", "summary", "save_as"?, "unverified"?}
  {"target": "ship_order", "pk", "set": {...}, "summary"}
A c7 body may contain "{address_id}", filled from an earlier action's save_as.
"unverified" marks a body whose field shape the public docs don't show; apply()
refuses it until a live read-only probe has confirmed the shape (CLUBDESK
["VERIFIED_SHAPES"]).
"""
from dataclasses import dataclass, field
from datetime import date

from clubdesk.models import Kind
from clubdesk.services import tax

# C7 enums (developer.commerce7.com/docs/club-memberships)
CANCEL_REASONS = {"Too much wine", "Financial reasons", "Health reasons", "Pregnant", "Moving",
                  "Switching to another club", "Other"}
HOLD_REASONS = {"Too much wine", "Financial reasons", "Health reasons", "Away from home", "Other"}

# ShipOrder statuses we may still change: not pooled, labeled or shipped.
CHANGEABLE_SHIP_STATUSES = {"new", "address_review", "ready", "on_hold"}


@dataclass
class OpenOrder:
    """A built, unshipped club order for this member (from shipping.ShipOrder)."""
    order_number: str
    ship_order_pk: int
    ship_status: str
    paid: bool
    delivery: str  # "Ship" / "Pickup"
    taxed_at: tax.TaxPlace
    package: str = ""


@dataclass
class Facts:
    today: date
    membership_id: str = ""
    customer_id: str = ""
    membership_status: str = ""
    delivery: str = ""
    open_orders: list = field(default_factory=list)
    taxed_states: frozenset = frozenset()
    current_package: str = ""


@dataclass
class Plan:
    stage: str
    actions: list = field(default_factory=list)
    manual_steps: list = field(default_factory=list)
    block_reasons: list = field(default_factory=list)
    note_text: str = ""


def _md(d):
    return f"{d.month}/{d.day}/{d.year}" if d else "?"


def _membership_put(facts, body, summary, unverified=False):
    a = {"target": "c7", "method": "PUT", "path": f"club-membership/{facts.membership_id}",
         "body": dict(body, isSendEmailConfirmation=False), "summary": summary}
    if unverified:
        a["unverified"] = "onHolds"
    return a


def _hold_body(start, end, reason):
    return {"status": "On Hold", "holdReason": reason if reason in HOLD_REASONS else "Other",
            "onHolds": [{"startDate": start.isoformat(), "endDate": end.isoformat()}]}


def _ship_order_set(order, values, summary, plan):
    if order.ship_status not in CHANGEABLE_SHIP_STATUSES:
        plan.block_reasons.append(
            f"Order #{order.order_number} is already {order.ship_status.replace('_', ' ')}; "
            "dissolve its pool or void the label first.")
        return
    plan.actions.append({"target": "ship_order", "pk": order.ship_order_pk, "set": values,
                         "summary": summary})


def plan(req, facts):
    open_orders = list(facts.open_orders)
    stage = "after_build" if open_orders else "before_build"
    p = Plan(stage=stage)
    d = req.details or {}
    when = req.requested_date
    pkg = (facts.current_package or d.get("package", "")
           or (open_orders[0].package if open_orders else ""))

    if req.kind in (Kind.SKIP, Kind.PAUSE, Kind.CANCEL, Kind.ADDRESS, Kind.HOLD_PICKUP) \
            and not facts.membership_id:
        p.block_reasons.append("No active club membership found for this email.")
        return p

    k = req.kind
    if k in (Kind.SKIP, Kind.PAUSE):
        if not when:
            p.block_reasons.append("Need the date the member wants to resume.")
            return p
        label = "Skip" if k == Kind.SKIP else "Pause"
        p.actions.append(_membership_put(
            facts, _hold_body(facts.today, when, d.get("reason", "Other")),
            f"{label}: put membership on hold until {_md(when)}", unverified=True))
        for o in open_orders:
            _ship_order_set(o, {"status": "on_hold", "hold_note": f"{label} requested; refund in C7"},
                            f"Hold order #{o.order_number} so it doesn't ship", p)
            p.manual_steps.append(f"Refund or cancel order #{o.order_number} in C7 admin.")
        p.note_text = f"{label} requested by email; on hold until {_md(when)}."

    elif k == Kind.CANCEL:
        reason = d.get("reason") if d.get("reason") in CANCEL_REASONS else "Other"
        p.actions.append(_membership_put(facts, {"status": "Cancelled", "cancellationReason": reason},
                                         f"Cancel membership (reason: {reason})"))
        for o in open_orders:
            _ship_order_set(o, {"status": "on_hold", "hold_note": "Member cancelled; refund in C7"},
                            f"Hold order #{o.order_number} so it doesn't ship", p)
            p.manual_steps.append(f"Refund order #{o.order_number} in C7 admin.")
        p.note_text = f"Membership cancelled at member's request by email (reason: {reason})."

    elif k == Kind.HOLD_PICKUP:
        standing = bool(d.get("standing"))
        if stage == "before_build":
            p.actions.append(_membership_put(facts, {"orderDeliveryMethod": "Pickup"},
                                             "Switch membership to Pickup"))
            if not standing:
                p.manual_steps.append(f"After {pkg or 'this release'} builds, switch the "
                                      "membership back to Ship.")
        for o in open_orders:
            if o.paid and o.delivery == "Ship":
                p.block_reasons += tax.check_change(tax.winery_place(),
                                                    [(o.order_number, o.taxed_at)],
                                                    facts.taxed_states)
            until = f" until {_md(when)}" if when else ""
            _ship_order_set(o, {"status": "on_hold", "hold_note": f"Hold for pickup{until}"},
                            f"Hold order #{o.order_number} for pickup{until}", p)
            p.manual_steps.append(f"Change order #{o.order_number} to Pickup in C7 admin and "
                                  "refund the shipping charge.")
        if open_orders and standing:
            p.actions.append(_membership_put(facts, {"orderDeliveryMethod": "Pickup"},
                                             "Switch membership to Pickup for future releases"))
        p.note_text = ("Hold for pickup requested by email"
                       + (f"; picking up by {_md(when)}." if when else "."))

    elif k == Kind.DELAY:
        if not when:
            p.block_reasons.append("Need the date the member wants delivery after.")
            return p
        for o in open_orders:
            _ship_order_set(o, {"requested_ship_date": when.isoformat()},
                            f"Hold order #{o.order_number} until {_md(when)}", p)
        if stage == "before_build":
            p.actions.append({"target": "pending_date", "summary":
                              f"Save {_md(when)}; set it as the requested ship date when "
                              f"{pkg or 'the next release'} is built"})
        p.note_text = (f"Requested delivery after {_md(when)}"
                       + (f" for {pkg}" if pkg else "") + " (by email).")

    elif k == Kind.ADDRESS:
        a = d.get("address") or {}
        missing = [f for f in ("street1", "city", "state", "zip") if not a.get(f)]
        if missing:
            p.block_reasons.append(f"Address is missing {', '.join(missing)}.")
            return p
        new = tax.TaxPlace.from_parts(a["state"], a["zip"])
        affected = [(o.order_number, o.taxed_at) for o in open_orders
                    if o.paid and o.delivery == "Ship" and d.get("include_open_order", True)]
        p.block_reasons += tax.check_change(new, affected, facts.taxed_states)
        p.actions.append({"target": "c7", "method": "POST", "path": "customer-address",
                          "save_as": "address_id",
                          "body": {"customerId": facts.customer_id,
                                   "firstName": a.get("first_name", req.first_name),
                                   "lastName": a.get("last_name", ""),
                                   "address": a["street1"], "address2": a.get("street2", ""),
                                   "city": a["city"], "stateCode": new.state,
                                   "zipCode": a["zip"], "countryCode": "US"},
                          "summary": f"Add address {a['street1']}, {a['city']} {new}"})
        p.actions.append(_membership_put(facts, {"shipToCustomerAddressId": "{address_id}"},
                                         "Use the new address for club shipments"))
        for o in open_orders:
            if d.get("include_open_order", True):
                p.manual_steps.append(f"Update the ship-to on order #{o.order_number} in C7 admin, "
                                      "then re-sync it on the Shipping page.")
        p.note_text = f"Club ship-to changed by email to {a['street1']}, {a['city']} {new}."

    elif k == Kind.SWAP:
        out_, in_ = d.get("remove", []), d.get("add", [])
        what = f"remove {', '.join(out_) or '?'}; add {', '.join(in_) or '?'}"
        if open_orders:
            for o in open_orders:
                p.manual_steps.append(f"Edit order #{o.order_number} in C7 admin: {what}. "
                                      "Charge or refund the price difference.")
        else:
            p.block_reasons.append("Changing club selections before the build needs the "
                                   "club-package route, which isn't verified yet.")
        p.note_text = f"Wine swap requested by email: {what}."

    elif k in (Kind.REFUND, Kind.CARD):
        p.manual_steps.append(d.get("instruction") or f"{req.get_kind_display()}: handle in C7 admin.")
        p.note_text = f"{req.get_kind_display()} requested by email."
    else:
        p.manual_steps.append("No change needed; answer the member.")

    return p
