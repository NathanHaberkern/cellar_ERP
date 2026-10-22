"""Read-only lookups that build plan.Facts for a request: GETs to Commerce7 and
reads of shipping.ShipOrder. Nothing here writes anywhere."""
from django.conf import settings
from django.utils import timezone

from clubdesk.services import tax
from clubdesk.services.plan import Facts, OpenOrder

UNSHIPPED = ("new", "address_review", "ready", "pooled", "labeled", "on_hold")


def find_customer(client, email):
    data = client.get("customer", {"q": email})
    email = email.strip().lower()
    for c in data.get("customers") or []:
        if any((e.get("email") or "").lower() == email for e in c.get("emails") or []):
            return c
    return None


def active_membership(customer):
    """The member's live club membership from customer.clubs (Active first, then On Hold)."""
    clubs = customer.get("clubs") or []
    for wanted in ("Active", "On Hold"):
        for m in clubs:
            if m.get("status") == wanted:
                return m
    return None


def taxed_states(client):
    data = client.get("tax")
    return frozenset((t.get("stateCode") or "").upper() for t in data.get("taxes") or []
                     if t.get("stateCode"))


def open_orders(customer_id):
    from shipping.models import ShipOrder

    out = []
    for o in ShipOrder.objects.filter(c7_customer_id=customer_id, is_club=True,
                                      status__in=UNSHIPPED):
        payload = o.c7_payload or {}
        delivery = payload.get("orderDeliveryMethod") or "Ship"
        place = (tax.winery_place() if delivery == "Pickup"
                 else tax.TaxPlace.from_parts(o.ship_state, o.ship_postal_code))
        out.append(OpenOrder(order_number=o.c7_order_number, ship_order_pk=o.pk,
                             ship_status=o.status, paid=payload.get("paymentStatus") == "Paid",
                             delivery=delivery, taxed_at=place, package=o.club_package))
    return out


def facts_for(req, client):
    customer = find_customer(client, req.customer_email)
    if not customer:
        return Facts(today=timezone.localdate())
    m = active_membership(customer) or {}
    orders = open_orders(customer["id"])
    return Facts(
        today=timezone.localdate(),
        customer_id=customer["id"],
        membership_id=m.get("clubMembershipId", ""),
        membership_status=m.get("status", ""),
        open_orders=orders,
        taxed_states=taxed_states(client) if settings.CLUBDESK.get("CHECK_TAX_STATES", True)
        else frozenset(),
        current_package=req.details.get("package", "") or (orders[0].package if orders else ""),
    )
