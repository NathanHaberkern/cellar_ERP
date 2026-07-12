"""
TTB F 5120.17 — Report of Wine Premises Operations, Part I read-layer.

Reconstructs every (section, column, line) figure for a period from the append-only
event ledger, carrying the prior period's ending inventory forward as the opening.
Produces figures keyed to the form's own field names (a1.1, b1.31, …) so the output
can drive a data export or, later, fill the actual PDF.

ROUNDING: gallons are summed at full precision and rounded once, here at the report
boundary, to the nearest tenth (27 CFR 24.281).

Column ↔ tax class:  a = not over 16%,  b = 16–21%,  c = 21–24%.
Section 1 = A (bulk),  Section 2 = B (bottled).
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from cellar.models import (
    BookToBond, FortificationEvent, BottlingRun, TaxPaidRemoval,
    BondTransfer, SweeteningEvent, BondAdjustment, VolumeLoss, LotLineage,
)

COLS = ("a", "b", "c")
TENTH = Decimal("0.1")


def _q(x):
    return Decimal(str(x)).quantize(TENTH, rounding=ROUND_HALF_UP)


def _period(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def lot_tax_class(lot):
    """A lot's tax class: fortified class if fortified, else its book-to-bond class, else 'a'."""
    fe = lot.fortifications.filter(voided_at__isnull=True).order_by("-booked_at").first()
    if fe:
        return fe.expected_tax_class
    bb = lot.bond_bookings.filter(voided_at__isnull=True).order_by("-booked_at").first()
    if bb:
        return bb.tax_class
    return "a"


def build_5120_17(year, month, opening_bulk=None, opening_bottled=None):
    """opening_* : {col: gallons} carried from the prior period's ending (or seed)."""
    start, end = _period(year, month)
    ob = {c: Decimal(str((opening_bulk or {}).get(c, 0))) for c in COLS}
    ot = {c: Decimal(str((opening_bottled or {}).get(c, 0))) for c in COLS}

    # accumulators: bulk[line][col], bottled[line][col]
    A = defaultdict(lambda: defaultdict(Decimal))   # Section A (bulk)
    B = defaultdict(lambda: defaultdict(Decimal))   # Section B (bottled)

    def in_period(qs, field):
        return qs.filter(voided_at__isnull=True, **{f"{field}__gte": start, f"{field}__lt": end})

    # A2 produced by fermentation  (book-to-bond)
    for bb in in_period(BookToBond.objects, "booked_at"):
        A[2][bb.tax_class] += bb.gallons_produced or 0
    # A4  produced by addition of spirits (the finished wine, e.g. col b Port)
    # A19 used for addition of spirits (the base)
    # A2  produced by fermentation — INITIAL fortifications only
    #
    # An INITIAL fortification is the production booking for a Port lot: the base
    # wine has just fermented, is under 16%, and has never been booked to bond, so it
    # is produced into col (a) line 2 and used out of col (a) line 19 in the same
    # period. It self-zeroes, which is why St. Amant's filed reports — which put the
    # base in col (b) — still balanced. Col (a) is the correct classification.
    #
    # An ADJUSTMENT (spring racking) produces nothing: the base is already in bond and
    # already in a class. Line 19 lands in the base's OWN class and line 2 stays empty.
    # Reporting it as fermentation would invent production that never happened.
    for fe in in_period(FortificationEvent.objects, "booked_at"):
        base_col = getattr(fe, "base_tax_class", "a") or "a"
        A[4][fe.expected_tax_class] += fe.finished_wg or 0
        A[19][base_col] += fe.base_wg or 0
        if getattr(fe, "kind", "initial") == "initial":
            A[2][base_col] += fe.base_wg or 0
    # A3 produced by sweetening / A18 used for sweetening
    for sw in in_period(SweeteningEvent.objects, "sweetened_at"):
        A[3][sw.tax_class] += sw.volume_produced
        A[18][sw.tax_class] += sw.volume_used
    # A13 bottled  (bulk → bottled) ; B2 bottled ; A29 bottling loss (bulk out = bottled + loss)
    for run in in_period(BottlingRun.objects, "bottled_at"):
        col = lot_tax_class(run.source_lot)
        A[13][col] += run.volume_bottled_gal
        B[2][col] += run.volume_bottled_gal
        if run.bottling_loss_gal:
            A[29][col] += run.bottling_loss_gal
    # transfers in bond  (A15 out / A7 in bulk ; B9 out / B3 in bottled)
    for t in in_period(BondTransfer.objects, "transferred_at"):
        tgt = A if t.phase == "bulk" else B
        if t.direction == "out":
            tgt[15 if t.phase == "bulk" else 9][t.tax_class] += t.gallons
        else:
            tgt[7 if t.phase == "bulk" else 3][t.tax_class] += t.gallons
    # B8 removed taxpaid (bottled)
    for r in in_period(TaxPaidRemoval.objects, "removed_at"):
        col = lot_tax_class(r.bottling_run.source_lot)
        B[8][col] += r.wine_gallons_removed
    # A14 removed taxpaid (bulk)
    from cellar.models import BulkTaxPaidRemoval
    for r in in_period(BulkTaxPaidRemoval.objects, "removed_at"):
        A[14][r.tax_class] += r.wine_gallons
    # A29 LOSSES (OTHER THAN INVENTORY) — evaporation, topping, racking, angel's share.
    # NOT line 30: that is INVENTORY LOSSES, i.e. a shortage found when you take a
    # physical inventory. A known, dated, measured loss is a line 29 loss. (Line 30 is
    # reached through BondAdjustment(kind=inventory_loss), below.)
    for vl in in_period(VolumeLoss.objects, "occurred_at"):
        A[29][lot_tax_class(vl.lot)] += vl.volume_gal
    # misc adjustments
    adj_map_A = {"inventory_loss": 30, "inventory_gain": 9, "dump_to_bulk": 8, "testing": 23}
    adj_map_B = {"tasting": 11, "family_use": 13, "taxpaid_return": 4, "breakage": 18,
                 "inventory_loss": 19, "dump_to_bulk": 10, "export": 12, "testing": 14}
    for a in in_period(BondAdjustment.objects, "occurred_at"):
        if a.phase == "bulk" and a.kind in adj_map_A:
            A[adj_map_A[a.kind]][a.tax_class] += a.gallons
        elif a.phase == "bottled" and a.kind in adj_map_B:
            B[adj_map_B[a.kind]][a.tax_class] += a.gallons

    # ---- assemble, compute totals + ending, per column ----
    A_INC = [2, 3, 4, 5, 6, 7, 9]                # + on-hand begin (line 1)
    A_DEC = [13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 29, 30]
    B_INC = [2, 3, 4]
    B_DEC = [8, 9, 10, 11, 12, 13, 14, 18, 19]

    lines = {}   # field_name -> Decimal
    for col in COLS:
        # Section A
        A[1][col] = ob[col]
        total_in = ob[col] + sum(A[l][col] for l in A_INC)
        dec = sum(A[l][col] for l in A_DEC)
        A[31][col] = total_in - dec          # on hand end
        A[12][col] = total_in
        A[32][col] = A[31][col] + dec        # must equal line 12
        # Section B
        B[1][col] = ot[col]
        b_total_in = ot[col] + sum(B[l][col] for l in B_INC)
        b_dec = sum(B[l][col] for l in B_DEC)
        B[20][col] = b_total_in - b_dec
        B[7][col] = b_total_in
        B[21][col] = B[20][col] + b_dec
        for line, val in A.items():
            if val[col]:
                lines[f"{col}1.{line}"] = _q(val[col])
        for line, val in B.items():
            if val[col]:
                lines[f"{col}2.{line}"] = _q(val[col])

    ending_bulk = {c: _q(A[31][c]) for c in COLS}
    ending_bottled = {c: _q(B[20][c]) for c in COLS}
    balanced = all(_q(A[12][c]) == _q(A[32][c]) and _q(B[7][c]) == _q(B[21][c]) for c in COLS)
    return {
        "period": f"{year}-{month:02d}", "fields": lines,
        "ending_bulk": ending_bulk, "ending_bottled": ending_bottled, "balanced": balanced,
    }


# ============================================================ Part III / IV
def build_5120_17_part3(year, month, opening_pg=0):
    """Part III — Summary of Distilled Spirits (WINE SPIRITS, proof gallons).
    Reads the high-proof spirit ledger: receipts add, fortification draws use."""
    from cellar.models import HighProofSpiritLedger as H
    start, end = _period(year, month)
    rows = H.objects.filter(voided_at__isnull=True, event_date__gte=start, event_date__lt=end)
    received = sum((r.proof_gallons or 0) for r in rows if r.event_type == "receipt")
    used = sum((-(r.proof_gallons or 0)) for r in rows if r.event_type == "draw")
    opening = Decimal(str(opening_pg))
    on_hand_end = opening + received - used
    return {
        "line_1_on_hand_beginning": _q(opening),
        "line_2_received": _q(received),
        "line_4_total": _q(opening + received),
        "line_5_used": _q(used),
        "line_9_on_hand_end": _q(on_hand_end),
        "line_10_total": _q(used + on_hand_end),
    }


def build_5120_17_part4(year, month, opening_grapes_lbs=0, opening_concentrate_gal=0):
    """Part IV — Summary of Materials Received and Used.
    Grapes in pounds (col a); grape concentrate in gallons (col d)."""
    from cellar.models import WeighTag, MaterialTransaction
    start, end = _period(year, month)

    wt = WeighTag.objects.filter(harvest_event__harvest_date__gte=start,
                                 harvest_event__harvest_date__lt=end)
    grapes_received = sum((t.net_total for t in wt), Decimal(0))
    grapes_used = sum((t.net_total for t in wt if t.disposition == "crushed"), Decimal(0))
    g_open = Decimal(str(opening_grapes_lbs))
    grapes_end = g_open + grapes_received - grapes_used

    mt = MaterialTransaction.objects.filter(voided_at__isnull=True, occurred_at__gte=start,
                                            occurred_at__lt=end, material__kind="concentrate")
    conc_recv = sum((m.quantity for m in mt if m.direction == "received"), Decimal(0))
    conc_used = sum((m.quantity for m in mt if m.direction == "used"), Decimal(0))
    conc_destroyed = sum((m.quantity for m in mt if m.direction == "destroyed"), Decimal(0))
    c_open = Decimal(str(opening_concentrate_gal))
    conc_end = c_open + conc_recv - conc_used - conc_destroyed

    return {
        "grapes_lbs": {
            "line_1_on_hand_beginning": _q(g_open),
            "line_2_received": _q(grapes_received),
            "line_5_used_in_wine_production": _q(grapes_used),
            "line_9_on_hand_end": _q(grapes_end),
        },
        "concentrate_gal": {
            "line_1_on_hand_beginning": _q(c_open),
            "line_2_received": _q(conc_recv),
            "line_5_used_in_wine_production": _q(conc_used),
            "line_8_removed_destroyed": _q(conc_destroyed),
            "line_9_on_hand_end": _q(conc_end),
        },
    }
