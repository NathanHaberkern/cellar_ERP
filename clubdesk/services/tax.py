"""Tax guardrail for ship-to and delivery-method changes.

Commerce7 taxes a club order when the order is built, at the ship-to (or the
pickup location's) state and ZIP. Changing where a PAID order goes afterwards
would leave the tax collected for the old place, so it would be under- or
over-collected. California rates differ by city and district, so a different
ZIP is treated as a possibly different rate; C7's tax API exposes only state-level
settings, so we can't price the difference ourselves.

Rules:
  * Membership-only change, no paid unshipped club order: allowed. The next
    build taxes at the new address.
  * Paid unshipped order and the tax location changes (state, or ZIP): blocked.
    Nate re-taxes the order in C7 admin, or refunds and rebills.
  * New state we have no tax set up for in C7: blocked, whatever the order state.
"""
from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class TaxPlace:
    state: str
    zip5: str

    @classmethod
    def from_parts(cls, state, zip_code):
        return cls((state or "").strip().upper(), (zip_code or "").strip()[:5])

    def __str__(self):
        return f"{self.state} {self.zip5}".strip()


def winery_place():
    c = settings.CLUBDESK
    return TaxPlace.from_parts(c["PICKUP_STATE"], c["PICKUP_ZIP"])


def check_change(new, paid_open_orders, taxed_states):
    """Block reasons for moving the tax location to `new` (a TaxPlace).

    paid_open_orders: [(order_number, TaxPlace it was taxed at)] for paid, unshipped
    club orders this change would affect. taxed_states: states C7 has tax set up for.
    Returns [] when the change is safe.
    """
    reasons = []
    if new.state and taxed_states and new.state not in taxed_states:
        reasons.append(f"Commerce7 has no tax set up for {new.state}; we may not ship there. "
                       "Check compliance before changing the address.")
    for number, taxed_at in paid_open_orders:
        if taxed_at.state != new.state or taxed_at.zip5 != new.zip5:
            reasons.append(
                f"Order #{number} was paid with tax for {taxed_at}; sending it to {new} "
                "would under- or over-collect tax. Re-tax the order in C7 admin, or refund "
                "and rebill, then re-run this request.")
    return reasons
