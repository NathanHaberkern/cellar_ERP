"""
Part X — "Explain any unusual operations" (the form's own instruction 3).

Nobody writes this until the report is due, and by then the operation is four
weeks old and the person who did it is on a forklift. So generate it from the
ledger, in the period's own language, and let Nate edit rather than compose.

Four triggers, each tied to a line pair a TTB specialist would otherwise have to
ask about:

  1. INITIAL FORTIFICATION — production appears in col (a) line 2 and vanishes on
     line 19 in the same period, and a finished volume appears in col (b) line 4
     that never fermented. Reads as an error unless you say the word "Port".

  2. ALCOHOL ADJUSTMENT — col (b) line 4 production with no line 2 anywhere. Wine
     produced out of thin air, as far as the form shows.

  3. STRADDLE — spirits used in one period (Part III), the wine they made booked
     in another (Part I line 4). The two reports don't tie to each other, on
     purpose. `FortificationEvent.needs_part_x` already flags it.

  4. CHANGE OF TAX CLASS — cross-class blending, lines 5 / 20. Footnote 5/ on the
     form: "Only report blending if wines of different tax classes are blended
     together." Which is the only time we report it.

Also surfaces the routine-but-explainable: material losses, and the angel's-share
accrual (an ESTIMATE landing on line 29 — say so, unprompted, every time).
"""
from datetime import date
from decimal import Decimal

from cellar.models import (
    FortificationEvent, LotLineage, VolumeLoss,
)
from cellar.services.reporting import _period, lot_tax_class

COL = {"a": "col (a), not over 16%", "b": "col (b), 16–21%", "c": "col (c), 21–24%"}

# Which line pair carries a change of tax class. The form has purpose-built lines
# — 5 PRODUCED BY BLENDING / 20 USED FOR BLENDING, footnote 5/ — but a filer who
# prefers the write-in lines can flip this to (10, 20).
BLEND_LINES = (5, 20)

ACCRUAL_MARK = "angel's share accrual"


def _g(x):
    return f"{Decimal(str(x)):,.1f}" if x is not None else "?"


def build_part_x(year, month):
    """Returns {'narrative': str, 'entries': [ {trigger, text, lines} ]}."""
    start, end = _period(year, month)
    entries = []

    def in_period(qs, field):
        return qs.filter(voided_at__isnull=True,
                         **{f"{field}__gte": start, f"{field}__lt": end})

    # ---------------------------------------------------- fortifications
    for fe in in_period(FortificationEvent.objects, "booked_at").select_related("lot"):
        base = COL.get(fe.base_tax_class, fe.base_tax_class)
        fin = COL.get(fe.expected_tax_class, fe.expected_tax_class)

        if fe.kind == FortificationEvent.Kind.INITIAL:
            text = (
                f"Lot {fe.lot.code} — Port, fortified on skins {fe.fortified_on_skins_date:%B %-d, %Y}. "
                f"{_g(fe.base_wg)} gal of base wine (under 16% alc./vol.) was produced by "
                f"fermentation and used for the addition of wine spirits in the same period, "
                f"and so is reported on both line 2 and line 19 of {base}. "
                f"{_g(fe.proof_gallons_drawn)} proof gallons of grape wine spirits "
                f"({_g(fe.spirit_proof)} proof, {_g(fe.spirit_wg)} wine gallons) were added, "
                f"producing {_g(fe.finished_wg)} gal reported on line 4 of {fin}. "
                f"Volume was determined {fe.booked_at:%B %-d, %Y}."
            )
            entries.append({"trigger": "initial_fortification", "lot": fe.lot.code,
                            "lines": ["A2", "A19", "A4", "Part III line 5"], "text": text})

        else:
            text = (
                f"Lot {fe.lot.code} — alcohol adjustment on racking, {fe.booked_at:%B %-d, %Y}. "
                f"{_g(fe.base_wg)} gal of previously-produced wine in {base} was racked and "
                f"re-fortified with {_g(fe.proof_gallons_drawn)} proof gallons of grape wine "
                f"spirits ({_g(fe.spirit_wg)} wine gallons), yielding {_g(fe.finished_wg)} gal. "
                f"No wine was produced by fermentation: the base was already in bond, so it is "
                f"reported on line 19 of {base} and the result on line 4 of {fin}."
            )
            loss = fe.implied_loss
            if loss and loss > 0:
                text += (f" {_g(loss)} gal was lost on the rack and is reported on "
                         f"line 29 of {base}.")
            entries.append({"trigger": "alcohol_adjustment", "lot": fe.lot.code,
                            "lines": ["A19", "A4", "A29", "Part III line 5"], "text": text})

        if fe.needs_part_x:
            entries.append({
                "trigger": "straddle", "lot": fe.lot.code,
                "lines": ["A4", "Part III line 5"],
                "text": (
                    f"Lot {fe.lot.code} — the wine spirits were added "
                    f"{fe.fortified_on_skins_date:%B %-d, %Y} and the volume was not determined "
                    f"until {fe.booked_at:%B %-d, %Y}, in a later reporting period. The spirits "
                    f"use is reported in the period of addition (Part III) and the wine produced "
                    f"in the period the volume was determined (Part I line 4), so the two do not "
                    f"tie within a single report."),
            })

    # ---------------------------------------------- change of tax class
    for e in in_period(LotLineage.objects, "created_at").select_related(
            "parent_lot", "child_lot"):
        if e.relationship_type not in (LotLineage.Relationship.WHOLE_BLEND,
                                       LotLineage.Relationship.PARTIAL_BLEND):
            continue
        pc, cc = lot_tax_class(e.parent_lot), lot_tax_class(e.child_lot)
        if pc == cc:
            continue          # footnote 5/: only report blending ACROSS classes
        inc, dec = BLEND_LINES
        entries.append({
            "trigger": "change_of_tax_class",
            "lot": e.child_lot.code,
            "lines": [f"A{inc}", f"A{dec}"],
            "text": (
                f"Change of tax class — {_g(e.volume_gal)} gal of lot {e.parent_lot.code} "
                f"({COL.get(pc, pc)}) was blended into lot {e.child_lot.code} "
                f"({COL.get(cc, cc)}). Reported as used for blending on line {dec} of "
                f"{COL.get(pc, pc)} and produced by blending on line {inc} of "
                f"{COL.get(cc, cc)}."),
        })

    # -------------------------------------------------- estimated losses
    accruals = [vl for vl in in_period(VolumeLoss.objects, "occurred_at")
                if ACCRUAL_MARK in (vl.reason or "")]
    if accruals:
        total = sum((Decimal(str(vl.volume_gal)) for vl in accruals), Decimal("0"))
        lots = sorted({vl.lot.code for vl in accruals})
        entries.append({
            "trigger": "accrued_evaporation",
            "lot": ", ".join(lots),
            "lines": ["A29"],
            "text": (
                f"Losses reported on line 29 include {_g(total)} gal of ESTIMATED evaporation "
                f"on Port held in bond without topping ({', '.join(lots)}). These barrels are "
                f"aged for a decade or more and are not topped; the estimate is accrued annually "
                f"and is reconciled to the actual gauge when the barrels are emptied, with any "
                f"difference reported in that period."),
        })

    narrative = "\n\n".join(e["text"] for e in entries)
    return {"period": f"{year}-{month:02d}", "entries": entries, "narrative": narrative}
