"""Confirmation replies in Nate's voice. Text only: the Outlook draft is created
from this text by the email run, in the member's thread, and never sent by us.

Style (from 30 of Nate's sent club replies, Oct 2026):
  * first name and a comma on its own line; couples "Jim & Mary Ann,"
  * open with the confirmation ("You've got it!"), then one or two short lines
    saying what was done and what happens next
  * dates as 10/26, no year; spell the month for far-off dates ("February")
  * "Thanks," then two blank lines and the signature; no "Best regards",
    no "please don't hesitate", no bullets
"""
SIGNATURE = "Nathan Haberkern\nSt. Amant Winery\n(209) 367-0646\nnathan@stamantwine.com"


def _md(d):
    return f"{d.month}/{d.day}"


def _when(d, today):
    """10/26 for dates within ~3 months, 'February' beyond that."""
    if (d - today).days > 90:
        return d.strftime("%B")
    return _md(d)


def body_lines(req, plan, today):
    from clubdesk.models import Kind

    k, d, when = req.kind, req.details or {}, req.requested_date
    after = plan.stage == "after_build"
    if k == Kind.SKIP:
        return ["You've got it! We will skip this release"
                + (f" and reach out again in {when.strftime('%B')}." if when else ".")]
    if k == Kind.PAUSE:
        first = "I've refunded this order and placed a hold on your membership." if after \
            else "I've placed a hold on your membership."
        return [first + (f" We will reach out again in {when.strftime('%B')} to see if you are "
                         "ready for more wine." if when else "")]
    if k == Kind.CANCEL:
        return ["We have cancelled your club and refunded this order." if after
                else "We have cancelled your club."]
    if k == Kind.HOLD_PICKUP:
        line = "You've got it! We will hold your wine for pickup"
        line += f" until {_when(when, today)}." if when else "."
        return [line, "I've refunded the shipping charge." if after else ""]
    if k == Kind.DELAY:
        return [f"You've got it! We will have your wine delivered after {_when(when, today)}."]
    if k == Kind.ADDRESS:
        a = d.get("address") or {}
        return [f"You've got it! We've updated your club shipping address to {a.get('street1')} "
                f"in {a.get('city')}."]
    if k == Kind.SWAP:
        add = " and ".join(d.get("add") or [])
        return [f"Absolutely! We've swapped your wines{' for the ' + add if add else ''}."]
    return ["You've got it!"]


def draft_text(req, plan, today):
    name = req.first_name or (req.customer_name.split()[0] if req.customer_name else "")
    lines = [l for l in body_lines(req, plan, today) if l]
    greeting = f"{name},\n\n" if name else ""
    return f"{greeting}" + "\n".join(lines) + f"\nThanks,\n\n\n{SIGNATURE}"
