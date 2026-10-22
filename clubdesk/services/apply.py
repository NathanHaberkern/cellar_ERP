"""Apply an approved request: write to Commerce7, update ShipOrder, add the account note.

Order of operations, and why:
  1. Refuse unless the request is APPROVED by someone with approve_club_changes,
     has no block reasons, and every c7 action has a verified field shape.
  2. C7 writes, in plan order. The first failure stops the run; nothing after it
     is sent, and the request is FAILED with the error.
  3. Local ShipOrder changes (holds, requested ship dates).
  4. The customer-account note (POST /note). Every applied request gets one, so
     the C7 account always shows what we changed and who approved it. A failed
     note marks the request FAILED even though the change itself went through.
Every C7 call is recorded as a C7Write row.
"""
import json
from datetime import date

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from clubdesk.models import C7Write, ClubRequest
from clubdesk.permissions import APPROVE_CHANGES, perm
from clubdesk.services.plan import CHANGEABLE_SHIP_STATUSES
from integrations.commerce7 import C7Error

S = ClubRequest.Status


class NotApproved(Exception):
    pass


class ApplyError(Exception):
    pass


def _fill(body, saved):
    text = json.dumps(body)
    for k, v in saved.items():
        text = text.replace("{" + k + "}", v)
    return json.loads(text)


def _record(req, method, path, body, user, *, status_code=None, response=None, ok=False,
            is_note=False):
    return C7Write.objects.create(request=req, method=method, path=path, body=body,
                                  status_code=status_code, response=response or {}, ok=ok,
                                  is_note=is_note, made_by=user)


def _call(client, req, user, method, path, body, is_note=False):
    try:
        resp = client.request(method, path, json=body)
    except C7Error as e:
        _record(req, method, path, body, user, status_code=e.status_code,
                response={"error": str(e), "body": e.body if isinstance(e.body, dict) else str(e.body)},
                is_note=is_note)
        raise
    _record(req, method, path, body, user, status_code=200, response=resp, ok=True, is_note=is_note)
    return resp


def check_ready(req, user):
    if not user.has_perm(perm(APPROVE_CHANGES)):
        raise NotApproved("You don't have permission to apply club changes.")
    if req.status != S.APPROVED or not req.approved_by_id:
        raise NotApproved("This request hasn't been approved.")
    if req.block_reasons:
        raise NotApproved("This request is blocked: " + " ".join(req.block_reasons))
    verified = set(settings.CLUBDESK.get("VERIFIED_SHAPES", ()))
    for a in req.c7_actions:
        if a.get("unverified") and a["unverified"] not in verified:
            raise NotApproved(f"The C7 field shape for '{a['unverified']}' hasn't been confirmed "
                              "on the live API yet.")
    if not req.c7_customer_id:
        raise NotApproved("No C7 customer to write the account note on.")


def note_body(req, user):
    who = user.get_full_name() or user.get_username()
    text = (f"Club desk: {req.note_text} Email from {req.customer_email} on "
            f"{timezone.localtime(req.received_at):%m/%d/%Y}. Approved by {who}.")
    return {"type": "Note", "customerId": req.c7_customer_id, "content": text,
            "noteDate": timezone.now().isoformat()}


def apply(req, user, client):
    check_ready(req, user)
    saved = {}
    try:
        for a in req.c7_actions:
            resp = _call(client, req, user, a["method"], a["path"], _fill(a["body"], saved))
            if a.get("save_as"):
                rec = resp.get("customerAddress", resp) if isinstance(resp, dict) else {}
                saved[a["save_as"]] = rec.get("id", "")
        with transaction.atomic():
            _apply_ship_orders(req)
        _call(client, req, user, "POST", "note", note_body(req, user), is_note=True)
    except (C7Error, ApplyError) as e:
        req.status, req.error = S.FAILED, str(e)
        req.save(update_fields=["status", "error", "updated_at"])
        return False
    req.status, req.applied_at, req.error = S.APPLIED, timezone.now(), ""
    req.save(update_fields=["status", "applied_at", "error", "updated_at"])
    return True


def _apply_ship_orders(req):
    from shipping.models import ShipOrder

    for a in req.actions:
        if a.get("target") != "ship_order":
            continue
        order = ShipOrder.objects.select_for_update().get(pk=a["pk"])
        if order.status not in CHANGEABLE_SHIP_STATUSES:
            raise ApplyError(f"Order #{order.c7_order_number} was {order.get_status_display().lower()} "
                             "after this was planned; the C7 change went through, the hold did not.")
        for field, value in a["set"].items():
            if field == "requested_ship_date":
                value = date.fromisoformat(value)
            setattr(order, field, value)
        order.save()
