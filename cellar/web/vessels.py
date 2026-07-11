"""
Vessel picker — shared by the Movement panel and the Fermentation press step.

Both screens had the same job and got it wrong in opposite directions:

  * Movement filtered occupied tanks OUT of the dropdown server-side, so the
    "Allow co-occupancy" checkbox could never actually reach one — it was dead.
  * Press listed EVERY vessel with no checkbox at all, so picking an occupied tank
    just threw "SS-1 is occupied by 26V1" (and it offered fruit bins, which you
    can't press into).

One helper now serves both. Every candidate vessel is offered; occupied ones show
the lot that's in them and are disabled until the user ticks co-occupancy, at which
point they become selectable and `allow_blend` is passed through to
`operations.transfer_lot`. The cellar can always SEE the full tank farm — the
checkbox governs whether they may blend into it, which is the actual decision.

Type note: the totes (SS-Tote 1 / 2, 450 gal) are typed `tank` in the vessel table,
so filtering to TANK yields tanks AND totes while correctly excluding the macro /
1-ton fruit bins. That is exactly what press wants.
"""
from cellar.models import Vessel

from .tankmap import _open_assignments

VESSEL_TYPES = (Vessel.Type.TANK,)   # tanks + totes; excludes fruit bins


def vessel_options(exclude_lot=None):
    """[{vessel, occupied_by}] for every tank/tote, empties first.

    `occupied_by` is the code of the lot currently in the vessel, else None. A
    vessel already holding `exclude_lot` reads as empty — a lot does not blend
    with itself.
    """
    current = _open_assignments()                 # {vessel_id: Lot}
    rows = []
    for v in Vessel.objects.filter(type__in=VESSEL_TYPES).order_by("code"):
        holder = current.get(v.id)
        occupied_by = None
        if holder is not None and (exclude_lot is None or holder.pk != exclude_lot.pk):
            occupied_by = holder.code
        rows.append({"vessel": v, "occupied_by": occupied_by})
    rows.sort(key=lambda r: (r["occupied_by"] is not None, r["vessel"].code))
    return rows
