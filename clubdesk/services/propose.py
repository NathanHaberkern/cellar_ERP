"""Load parsed requests and turn them into proposals waiting for Nate.

The email run (Claude reading info@ and nathan@) writes one JSON object per
member request; load() stores them, propose() looks up C7 + shipping (read-only),
plans the change and drafts the reply. Nothing is written to Commerce7 here.
"""
from datetime import date

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from clubdesk.models import ClubRequest, Kind
from clubdesk.services import drafts, lookup
from clubdesk.services.plan import plan

S = ClubRequest.Status
FIELDS = ("mailbox", "message_id", "received_at", "customer_email", "customer_name", "first_name",
          "request_text", "kind", "details", "requested_date")


def load(records):
    """Create ClubRequests from parsed email records; skips message ids already loaded."""
    made = []
    for r in records:
        if ClubRequest.objects.filter(message_id=r["message_id"]).exists():
            continue
        if r["kind"] not in Kind.values:
            raise ValueError(f"unknown kind {r['kind']!r}")
        data = {k: r.get(k) for k in FIELDS if r.get(k) is not None}
        data["received_at"] = parse_datetime(r["received_at"]) or timezone.now()
        if r.get("requested_date"):
            data["requested_date"] = date.fromisoformat(r["requested_date"])
        made.append(ClubRequest.objects.create(**data))
    return made


def propose(req, client, facts=None):
    if req.status not in (S.NEW, S.PROPOSED, S.BLOCKED, S.FAILED):
        return req
    facts = facts or lookup.facts_for(req, client)
    p = plan(req, facts)
    req.c7_customer_id = facts.customer_id
    req.c7_membership_id = facts.membership_id
    req.stage = p.stage
    req.actions, req.manual_steps, req.block_reasons, req.note_text = (
        p.actions, p.manual_steps, p.block_reasons, p.note_text)
    if not facts.customer_id:
        req.block_reasons = ["No Commerce7 customer has this email address."]
    if facts.open_orders:
        from shipping.models import ShipOrder
        req.ship_order = ShipOrder.objects.filter(pk=facts.open_orders[0].ship_order_pk).first()
    req.draft_text = drafts.draft_text(req, p, facts.today)
    req.status = S.BLOCKED if req.block_reasons else S.PROPOSED
    req.approved_by, req.approved_at = None, None
    req.save()
    return req


def apply_pending_dates():
    """Delay requests approved before the build: once the club order lands in
    shipping, set its requested ship date. Returns the orders changed."""
    from shipping.models import ShipOrder

    changed = []
    for req in ClubRequest.objects.filter(kind=Kind.DELAY, status=S.APPLIED, stage="before_build",
                                          requested_date__isnull=False, ship_order__isnull=True):
        order = (ShipOrder.objects.filter(c7_customer_id=req.c7_customer_id, is_club=True,
                                          created_at__gte=req.received_at,
                                          status__in=("new", "address_review", "ready", "on_hold"))
                 .order_by("created_at").first())
        if not order:
            continue
        if not order.requested_ship_date or order.requested_ship_date < req.requested_date:
            order.requested_ship_date = req.requested_date
            order.save(update_fields=["requested_ship_date"])
        req.ship_order = order
        req.save(update_fields=["ship_order", "updated_at"])
        changed.append(order)
    return changed
