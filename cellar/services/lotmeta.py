"""
Where a lot's variety, program and block actually live.

They are not fields on Lot. The lot carries a LotDesignation whose `members` is a
JSON list of {abbr, seq}, and the abbreviation resolves through the curated
VarietalDesignation catalog back to (variety, program). The block comes the other
way, off the fruit: weigh-tag allocation → harvest event → block.

That's a deliberate design — the code is the thing the cellar says out loud, and
it has to survive a re-designation — but it means anything that wants to ask
"is this lot a port?" has to walk the same path. Doing that inline with
`lot__program=...` silently matches nothing, which is worse than failing. So it
lives here once.
"""
from cellar.models import Program, VarietalDesignation


def _abbrs(lot):
    d = lot.current_designation
    if d is None:
        return []
    return [m.get("abbr") for m in (d.members or []) if m.get("abbr")]


def designation_for(lot):
    """The VarietalDesignation behind the lot's first (primary) member."""
    for abbr in _abbrs(lot):
        vd = VarietalDesignation.objects.filter(abbreviation=abbr).first()
        if vd is not None:
            return vd
    return None


def lot_program(lot):
    vd = designation_for(lot)
    return vd.program if vd else None


def lot_variety(lot):
    vd = designation_for(lot)
    return vd.variety if vd else None


def is_port(lot):
    return lot_program(lot) == Program.PORT


def lot_block(lot):
    """The block the fruit came from, via the first weigh-tag allocation."""
    a = lot.allocations.filter(voided_at__isnull=True).select_related(
        "weigh_tag__harvest_event__block").first()
    if a is None:
        return None
    return getattr(a.weigh_tag.harvest_event, "block", None)
