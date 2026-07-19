"""
Historical vintage importer — 2023 / 2024 back-load.

WHAT THIS IS FOR
----------------
The 2025 vintage is being entered event-by-event through the live cellar workflow.
2023 and 2024 are not: they exist on paper and in spreadsheets, and the goal for
them is narrower — enough to answer three questions and survive an audit:

    how many gallons did we make, where did they go, what did they cost?

So this importer deliberately does NOT reconstruct the cellar. No vessels, no tank
assignments, no barrel placements, no readings, no pump-overs. It writes the
skeleton that carries volume, tax class, genealogy, and dollars:

    HarvestEvent → WeighTag → Lot → WeighTagAllocation       (tons in, fruit cost)
    VolumeMeasurement(stated) → BookToBond | FortificationEvent  (gallons, tax class)
    LotLineage                                                (blend genealogy)
    BottlingRun → BottlingDryGoodUse                          (cases, dry goods)
    TaxPaidRemoval | BulkTaxPaidRemoval | BondTransfer | MustSale | BondAdjustment
    LotCostAdjustment                                         (oak + overhead)

FIVE THINGS THAT ARE NOT OBVIOUS
--------------------------------
1. LOT CODES ARE FORCED. `generator.create_lot(override_code=...)` is used so the
   codes in the system are the codes on the paper. That bypasses LotSequenceCounter,
   which is correct here and wrong everywhere else: a 2023 lot must not consume a
   2025 sequence number, and an auditor pulling "23TR2" must find 23TR2.

2. SPIRIT RECEIPTS LOAD BEFORE FORTIFICATIONS, ALWAYS. FortificationEvent.save()
   raises if the draw exceeds HighProofSpiritLedger.on_hand_wg(). Files are processed
   in numbered order for exactly this reason, and within a file, rows are sorted by
   date — an out-of-order draw fails against a balance that had not been received yet.

3. BLENDS ARE WRITTEN AS RAW LotLineage EDGES, not through services.blending.blend().
   The blend service enforces live-workflow invariants (vessel co-occupancy, source
   balance, tax-class matrix) against state this import does not create. Writing the
   edge directly records the genealogy — which is what costing and composition read —
   without fighting guards meant for a cellar that exists right now. This is the one
   place the importer goes around a service, and it is on purpose.

4. CARRY-IN LOTS ARE REAL LOTS. Wine in bond on the opening date from 2022-and-earlier
   is imported as lots with a STATED BookToBond, not as an inventory adjustment. It has
   to be lots: wine made in 2022 that got bottled in 2023 needs something for the
   BottlingRun to point at, and every removal model FKs a Lot or a BottlingRun.

5. EVERYTHING IS IDEMPOTENT BY NATURAL KEY. Re-running a file does not duplicate:
   lots key on code, weigh tags on tag number, bottling runs on (sku, date, lot),
   removals on (run, date, cases, channel). A partially-failed import is fixed by
   correcting the CSV and re-running, not by hand-unpicking an append-only ledger.

FILE ORDER (enforced)
    00_opening_inventory.csv   carry-in lots as of the opening date
    01_spirit_receipts.csv     HPGS receipts — before any fortification
    02_fruit.csv               harvest → weigh tag → lot → allocation
    03_production.csv          gallons produced + tax class (+ per-lot oak $)
    04_blends.csv              lineage edges
    05_bottling.csv            bottling runs + dry goods
    06_removals.csv            taxpaid, bulk, in-bond, must, adjustments
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from cellar.models import (
    BondAdjustment, BondTransfer, BookToBond, BottleFormat, BottlingDryGoodUse,
    BottlingRun, BulkTaxPaidRemoval, DryGood, ExternalDestination,
    FortificationEvent, HarvestEvent,
    HighProofSpiritLedger, Lot, LotCostAdjustment, LotLineage, MustSale, Program,
    TaxPaidRemoval, Variety, VolumeMeasurement, WeighTag, WeighTagAllocation,
)
from cellar.services import fortification as fort_svc
from cellar.services import generator

TENTH = Decimal("0.1")
CENT = Decimal("0.01")

FILE_ORDER = [
    "00_opening_inventory.csv",
    "01_spirit_receipts.csv",
    "02_fruit.csv",
    "03_production.csv",
    "04_blends.csv",
    "05_bottling.csv",
    "06_removals.csv",
]


class RowError(Exception):
    """A single CSV row could not be read. Carries the file and line for the report."""


# ----------------------------------------------------------------- parsing helpers
def dec(v, field="value"):
    v = (v or "").strip().replace(",", "").replace("$", "")
    if not v:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        raise RowError(f"{field}: '{v}' is not a number")


def req_dec(v, field):
    d = dec(v, field)
    if d is None:
        raise RowError(f"{field} is required")
    return d


def date_(v, field="date"):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise RowError(f"{field}: '{v}' is not a date (use YYYY-MM-DD)")


def req_date(v, field):
    d = date_(v, field)
    if d is None:
        raise RowError(f"{field} is required")
    return d


def text(v):
    return (v or "").strip()


def choice(v, allowed, field, default=None):
    val = text(v).lower()
    if not val:
        if default is not None:
            return default
        raise RowError(f"{field} is required (one of: {', '.join(allowed)})")
    if val not in allowed:
        raise RowError(f"{field}: '{val}' is not one of {', '.join(allowed)}")
    return val


def aware(d):
    """Midnight on `d` in the project timezone. VolumeMeasurement.measured_at is a
    DateTimeField and USE_TZ is on, so a bare datetime.combine() lands as naive and
    Django warns — and worse, a naive UTC read of a Pacific date can slide the
    measurement into the previous reporting month."""
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


def read_csv(path):
    """Yield (line_number, row_dict) for non-blank, non-comment rows."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            if not any(text(v) for v in row.values()):
                continue
            if text(next(iter(row.values()), "")).startswith("#"):
                continue
            yield i, row


# ----------------------------------------------------------------- lookup helpers
def find_lot(code):
    """Lot.code is a derived @property, so this is a Python-side scan. Fine at
    ~200 historical lots; cached per-run by the Importer."""
    code = text(code)
    if not code:
        return None
    for lot in Lot.objects.select_related("current_designation").all():
        if lot.code == code:
            return lot
    return None


def abbr_for(code, vintage_year):
    """Split the 2-digit vintage prefix off a paper lot code.

    generator.assign_initial_designation(override_code=…) stores the string as the
    designation's `abbr`, and render_designation() then prepends the vintage itself.
    Passing "23VERD1" straight through therefore renders "2323VERD1". The CSV takes
    the code exactly as it appears on the paper — "23VERD1" — and this strips the
    prefix so it round-trips to the same string.

    A mismatch is a typo worth stopping for: "23VERD1" on a vintage_year=2024 row
    means one of the two columns is wrong, and guessing which produces a lot that is
    findable by neither code.
    """
    vv = f"{int(vintage_year) % 100:02d}"
    if not code.startswith(vv):
        raise RowError(
            f"lot_code '{code}' does not start with '{vv}', the 2-digit form of "
            f"vintage_year {vintage_year}. Fix whichever column is wrong.")
    rest = code[2:]
    if not rest:
        raise RowError(f"lot_code '{code}' is only a vintage prefix")
    return rest


def get_variety(name):
    name = text(name)
    if not name:
        raise RowError("variety is required")
    v = Variety.objects.filter(name__iexact=name).first()
    if v is None:
        known = ", ".join(Variety.objects.values_list("name", flat=True)[:8])
        raise RowError(f"unknown variety '{name}' — seed it first (have: {known}…)")
    return v


def get_program(v, default=Program.TABLE):
    val = text(v).lower()
    if not val:
        return default
    aliases = {"table": Program.TABLE, "port": Program.PORT, "rose": Program.ROSE,
               "rosé": Program.ROSE, "rosado": Program.ROSE}
    if val not in aliases:
        raise RowError(f"program: '{val}' is not table / port / rose")
    return aliases[val]


def get_destination(name):
    name = text(name)
    if not name:
        return None
    d = ExternalDestination.objects.filter(name__iexact=name).first()
    if d is None:
        d = ExternalDestination.objects.create(name=name)
    return d


def tax_class(v, default="a"):
    return choice(v, ("a", "b", "c"), "tax_class", default=default)


# ===================================================================== the importer
class Importer:
    """Parses every file, reports, then writes in one transaction.

    Two phases on purpose. A partial write into an append-only ledger is expensive
    to undo — voiding rows leaves them visible forever — so nothing is written until
    every row in every file has parsed and the cross-file references resolve.
    """

    def __init__(self, directory, *, stdout=None, vintages=(2023, 2024)):
        self.dir = directory
        self.out = stdout
        self.vintages = set(vintages)
        self.errors = []          # (file, line, message)
        self.warnings = []
        self.parsed = {}          # filename -> [row dicts]
        self.stats = defaultdict(int)
        self._lot_cache = {}

    # ------------------------------------------------------------------ reporting
    def w(self, msg=""):
        if self.out:
            self.out.write(msg)

    def err(self, fname, line, msg):
        self.errors.append((fname, line, str(msg)))

    # ---------------------------------------------------------------------- parse
    def parse_all(self):
        import os
        for fname in FILE_ORDER:
            path = os.path.join(self.dir, fname)
            if not os.path.exists(path):
                self.parsed[fname] = []
                self.warnings.append(f"{fname} not present — skipped")
                continue
            handler = getattr(self, "_parse_" + fname[3:].replace(".csv", ""))
            rows = []
            for line, raw in read_csv(path):
                try:
                    rows.append(handler(raw))
                except RowError as e:
                    self.err(fname, line, e)
                except Exception as e:  # noqa: BLE001
                    self.err(fname, line, f"{type(e).__name__}: {e}")
            self.parsed[fname] = rows
        self._cross_check()
        return not self.errors

    def _cross_check(self):
        """Resolve references between files before anything is written."""
        # every lot code that this import will bring into existence
        known = set()
        for r in self.parsed.get("00_opening_inventory.csv", []):
            known.add(r["lot_code"])
        for r in self.parsed.get("02_fruit.csv", []):
            known.add(r["lot_code"])
        for r in self.parsed.get("04_blends.csv", []):
            known.add(r["child_code"])
        # plus anything already in the database
        existing = {lot.code for lot in Lot.objects.select_related("current_designation")}
        known |= existing

        def check(fname, rows, field, label=None):
            for r in rows:
                code = r.get(field)
                if code and code not in known:
                    self.err(fname, r["_line"],
                             f"{label or field} '{code}' is not created by any file "
                             f"in this import and does not exist in the database")

        check("03_production.csv", self.parsed.get("03_production.csv", []), "lot_code")
        for r in self.parsed.get("04_blends.csv", []):
            for pc in r["parents"]:
                if pc["code"] not in known:
                    self.err("04_blends.csv", r["_line"],
                             f"parent lot '{pc['code']}' does not exist and is not "
                             f"created by this import")
        check("05_bottling.csv", self.parsed.get("05_bottling.csv", []), "lot_code")
        check("06_removals.csv", self.parsed.get("06_removals.csv", []), "lot_code")

        # a lot may not be booked to bond twice
        seen = {}
        for r in self.parsed.get("03_production.csv", []):
            c = r["lot_code"]
            if c in seen and r["method"] != "fortify_adjustment":
                self.err("03_production.csv", r["_line"],
                         f"lot '{c}' already has a production row on line {seen[c]}; "
                         f"only an 'fortify_adjustment' may repeat")
            seen[c] = r["_line"]

        # bottling runs may not remove more cases than they produced
        produced = {}
        for r in self.parsed.get("05_bottling.csv", []):
            produced[(r["sku"], r["bottled_at"])] = r["cases"]
        removed = defaultdict(int)
        for r in self.parsed.get("06_removals.csv", []):
            if r["kind"] == "taxpaid_bottled":
                removed[(r["sku"], r["run_bottled_at"])] += r["cases"]
        for key, cases in removed.items():
            made = produced.get(key)
            if made is not None and cases > made:
                self.warnings.append(
                    f"06_removals: {key[0]} run {key[1]} — {cases} cases removed but only "
                    f"{made} produced. Check for a missing bottling row.")

    # ------------------------------------------------------------ per-file parsers
    def _parse_opening_inventory(self, r):
        code = text(r.get("lot_code"))
        if not code:
            raise RowError("lot_code is required — this is the code on the paper record")
        vintage = int(req_dec(r.get("vintage_year"), "vintage_year"))
        return {
            "_line": 0, "lot_code": code, "abbr": abbr_for(code, vintage),
            "vintage_year": vintage,
            "variety": get_variety(r.get("variety")),
            "program": get_program(r.get("program")),
            "as_of": req_date(r.get("as_of_date"), "as_of_date"),
            "gallons": req_dec(r.get("gallons"), "gallons"),
            "tax_class": tax_class(r.get("tax_class")),
            "cost_basis": dec(r.get("cost_basis_total"), "cost_basis_total"),
            "notes": text(r.get("notes")),
        }

    def _parse_spirit_receipts(self, r):
        wg = req_dec(r.get("wine_gallons"), "wine_gallons")
        proof = req_dec(r.get("proof"), "proof")
        if wg <= 0:
            raise RowError("wine_gallons must be positive on a receipt")
        return {
            "_line": 0,
            "event_date": req_date(r.get("event_date"), "event_date"),
            "wine_gallons": wg, "proof": proof,
            "proof_gallons": dec(r.get("proof_gallons")) or (wg * proof / 100).quantize(CENT),
            "supplier": text(r.get("supplier")),
            "shipment_ref": text(r.get("shipment_ref")),
            "cost": dec(r.get("total_cost"), "total_cost"),
            "notes": text(r.get("notes")),
        }

    def _parse_fruit(self, r):
        code = text(r.get("lot_code"))
        if not code:
            raise RowError("lot_code is required")
        lbs = dec(r.get("net_lbs"), "net_lbs")
        tons = dec(r.get("net_tons"), "net_tons")
        if lbs is None and tons is None:
            raise RowError("give net_lbs or net_tons")
        if lbs is None:
            lbs = (tons * 2000).quantize(TENTH)
        src = choice(r.get("source_type"), ("estate", "purchased", "contract"),
                     "source_type", default="purchased")
        vintage = int(req_dec(r.get("vintage_year"), "vintage_year"))
        return {
            "_line": 0, "lot_code": code, "abbr": abbr_for(code, vintage),
            "vintage_year": vintage,
            "variety": get_variety(r.get("variety")),
            "program": get_program(r.get("program")),
            "vineyard_name": text(r.get("vineyard")),
            "block_name": text(r.get("block")),
            "harvest_date": req_date(r.get("harvest_date"), "harvest_date"),
            "weigh_tag_number": text(r.get("weigh_tag_number")) or None,
            "source_type": src,
            "net_lbs": lbs,
            "brix": dec(r.get("brix_at_receipt")),
            "purchase_price_per_ton": dec(r.get("purchase_price_per_ton")),
            "fruit_cost_per_ton": dec(r.get("fruit_cost_per_ton")),
            "notes": text(r.get("notes")),
        }

    def _parse_production(self, r):
        code = text(r.get("lot_code"))
        if not code:
            raise RowError("lot_code is required")
        method = choice(r.get("method"),
                        ("book_to_bond", "fortify_initial", "fortify_adjustment"),
                        "method", default="book_to_bond")
        gallons = dec(r.get("gallons_produced"), "gallons_produced")
        pg = dec(r.get("pg_drawn"), "pg_drawn")
        base = dec(r.get("base_wg"), "base_wg")

        if method == "book_to_bond":
            if gallons is None:
                raise RowError("book_to_bond needs gallons_produced")
        else:
            if pg is None:
                raise RowError(f"{method} needs pg_drawn (proof gallons of spirit)")
            if gallons is None:
                raise RowError(f"{method} needs gallons_produced (the finished gauge, T)")
            if method == "fortify_adjustment" and base is None:
                raise RowError("fortify_adjustment needs base_wg — the gauge going in. "
                               "Deriving it would swallow the racking loss.")
        return {
            "_line": 0, "lot_code": code, "method": method,
            "booked_at": req_date(r.get("booked_at"), "booked_at"),
            "on_skins_date": date_(r.get("on_skins_date")),
            "gallons": gallons,
            "tax_class": tax_class(r.get("tax_class"),
                                   default="b" if method != "book_to_bond" else "a"),
            "pg_drawn": pg,
            "spirit_proof": dec(r.get("spirit_proof")),
            "base_wg": base,
            "oak_cost": dec(r.get("oak_cost"), "oak_cost"),
            "other_cost": dec(r.get("other_cost"), "other_cost"),
            "notes": text(r.get("notes")),
        }

    def _parse_blends(self, r):
        child = text(r.get("child_lot_code"))
        if not child:
            raise RowError("child_lot_code is required")
        parents = []
        for n in range(1, 9):
            pc = text(r.get(f"parent{n}_code"))
            if not pc:
                continue
            pg = dec(r.get(f"parent{n}_gallons"), f"parent{n}_gallons")
            if pg is None:
                raise RowError(f"parent{n}_code given without parent{n}_gallons")
            parents.append({"code": pc, "gallons": pg})
        if not parents:
            raise RowError("a blend needs at least one parent (parent1_code + parent1_gallons)")
        vintage = int(req_dec(r.get("vintage_year"), "vintage_year"))
        return {
            "_line": 0, "child_code": child, "abbr": abbr_for(child, vintage),
            "vintage_year": vintage,
            "variety": get_variety(r.get("variety")) if text(r.get("variety")) else None,
            "program": get_program(r.get("program")),
            "blended_at": req_date(r.get("blended_at"), "blended_at"),
            "relationship": choice(r.get("relationship"),
                                   ("whole_blend", "partial_blend_contribution",
                                    "split_saignee", "split_drainoff", "bottling_split"),
                                   "relationship", default="whole_blend"),
            "gallons": dec(r.get("child_gallons")),
            "tax_class": tax_class(r.get("tax_class")),
            "parents": parents,
            "notes": text(r.get("notes")),
        }

    def _parse_bottling(self, r):
        code = text(r.get("lot_code"))
        if not code:
            raise RowError("lot_code is required")
        fmt_name = text(r.get("bottle_format"))
        if not fmt_name:
            raise RowError("bottle_format is required (e.g. 750ml)")
        fmt = BottleFormat.objects.filter(name__iexact=fmt_name).first()
        if fmt is None:
            known = ", ".join(BottleFormat.objects.values_list("name", flat=True))
            raise RowError(f"unknown bottle_format '{fmt_name}' (have: {known or 'none'})")
        cases = req_dec(r.get("cases_produced"), "cases_produced")
        dry = []
        for n in range(1, 7):
            dn = text(r.get(f"drygood{n}_name"))
            if not dn:
                continue
            dg = DryGood.objects.filter(name__iexact=dn).first()
            if dg is None:
                raise RowError(f"unknown dry good '{dn}' — add it to the DryGood table first")
            dry.append({"dg": dg,
                        "qty": req_dec(r.get(f"drygood{n}_qty"), f"drygood{n}_qty")})
        return {
            "_line": 0, "lot_code": code, "format": fmt,
            "sku": text(r.get("sku")) or f"{code}-{fmt.name}",
            "bottled_at": req_date(r.get("bottled_at"), "bottled_at"),
            "bulk_gallons_in": dec(r.get("bulk_gallons_in")),
            "cases": int(cases),
            "line_labor_cost": dec(r.get("line_labor_cost")) or Decimal("0"),
            "dry_goods": dry,
            "notes": text(r.get("notes")),
        }

    def _parse_removals(self, r):
        kind = choice(r.get("kind"),
                      ("taxpaid_bottled", "taxpaid_bulk", "bond_transfer_out",
                       "bond_transfer_in", "must_sale", "adjustment"),
                      "kind")
        row = {
            "_line": 0, "kind": kind,
            "occurred_at": req_date(r.get("occurred_at"), "occurred_at"),
            "lot_code": text(r.get("lot_code")) or None,
            "sku": text(r.get("sku")) or None,
            "run_bottled_at": date_(r.get("run_bottled_at")),
            "cases": int(dec(r.get("cases")) or 0),
            "gallons": dec(r.get("gallons")),
            "tax_class": tax_class(r.get("tax_class")),
            "channel": choice(r.get("channel"), ("dtc", "wholesale", "other"),
                              "channel", default="wholesale"),
            "destination": text(r.get("destination")),
            "adjustment_kind": text(r.get("adjustment_kind")).lower() or None,
            "price_per_gallon": dec(r.get("price_per_gallon")),
            "notes": text(r.get("notes")),
        }
        if kind == "taxpaid_bottled":
            if not row["sku"] or not row["run_bottled_at"]:
                raise RowError("taxpaid_bottled needs sku + run_bottled_at to find the run")
            if row["cases"] <= 0:
                raise RowError("taxpaid_bottled needs cases")
        elif kind == "adjustment":
            valid = [c[0] for c in BondAdjustment.Kind.choices]
            if row["adjustment_kind"] not in valid:
                raise RowError(f"adjustment_kind must be one of: {', '.join(valid)}")
            if row["gallons"] is None:
                raise RowError("adjustment needs gallons")
        else:
            if row["gallons"] is None:
                raise RowError(f"{kind} needs gallons")
            if kind in ("taxpaid_bulk", "must_sale") and not row["lot_code"]:
                raise RowError(f"{kind} needs lot_code")
        return row

    # ---------------------------------------------------------------------- report
    def report(self):
        p = self.parsed
        self.w("\nParsed:")
        for f in FILE_ORDER:
            n = len(p.get(f, []))
            if n:
                self.w(f"  {n:>5}  {f}")

        prod = p.get("03_production.csv", [])
        gal = sum((r["gallons"] or 0) for r in prod)
        lbs = sum(r["net_lbs"] for r in p.get("02_fruit.csv", []))
        open_gal = sum(r["gallons"] for r in p.get("00_opening_inventory.csv", []))
        cases = sum(r["cases"] for r in p.get("05_bottling.csv", []))
        pg = sum((r["pg_drawn"] or 0) for r in prod)
        recv_pg = sum(r["proof_gallons"] for r in p.get("01_spirit_receipts.csv", []))

        self.w("\nTotals this import would create:")
        self.w(f"  fruit received .......... {lbs:>12,.1f} lb  ({float(lbs)/2000:,.2f} tons)")
        self.w(f"  opening inventory ....... {open_gal:>12,.1f} gal")
        self.w(f"  wine produced ........... {gal:>12,.1f} gal")
        self.w(f"  spirit received ......... {recv_pg:>12,.2f} PG")
        self.w(f"  spirit drawn ............ {pg:>12,.2f} PG")
        self.w(f"  cases bottled ........... {cases:>12,}")

        if pg > recv_pg + HighProofSpiritLedger.on_hand_pg():
            self.errors.append(
                ("01_spirit_receipts.csv", 0,
                 f"fortifications draw {pg} PG but only "
                 f"{recv_pg + HighProofSpiritLedger.on_hand_pg()} PG is receipted or on hand. "
                 f"FortificationEvent will refuse the draw — add the missing receipts."))

        # removals vs production, per lot — the "where did it go" sanity check
        self._volume_sanity()

        if self.warnings:
            self.w("\nWarnings (not fatal):")
            for m in self.warnings:
                self.w(f"  ! {m}")

        if self.errors:
            self.w("\nERRORS — nothing will be written:")
            for f, line, m in self.errors:
                where = f"{f}:{line}" if line else f
                self.w(f"  {where:<34} {m}")
        return not self.errors

    def _volume_sanity(self):
        """Flag lots that ship more than they made. Not fatal — a lot can legitimately
        be over-drawn on paper when a blend edge is missing — but it is the single
        most useful smell test on an import like this."""
        made = defaultdict(Decimal)
        for r in self.parsed.get("00_opening_inventory.csv", []):
            made[r["lot_code"]] += r["gallons"]
        for r in self.parsed.get("03_production.csv", []):
            if r["method"] != "fortify_adjustment":
                made[r["lot_code"]] += r["gallons"] or Decimal(0)
        out = defaultdict(Decimal)
        for r in self.parsed.get("04_blends.csv", []):
            for pc in r["parents"]:
                out[pc["code"]] += pc["gallons"]
        for r in self.parsed.get("05_bottling.csv", []):
            if r["bulk_gallons_in"]:
                out[r["lot_code"]] += r["bulk_gallons_in"]
        for r in self.parsed.get("06_removals.csv", []):
            if r["lot_code"] and r["gallons"]:
                out[r["lot_code"]] += r["gallons"]
        for code, shipped in sorted(out.items()):
            have = made.get(code)
            if have and shipped > have * Decimal("1.02"):
                self.warnings.append(
                    f"lot {code}: {shipped:,.1f} gal leaves it but only {have:,.1f} gal "
                    f"was produced/carried in ({shipped - have:,.1f} over)")

    # ----------------------------------------------------------------------- write
    @transaction.atomic
    def write(self, overhead_pool=None):
        self._write_opening()
        self._write_spirits()
        self._write_fruit()
        self._write_production()
        self._write_blends()
        self._write_bottling()
        self._write_removals()
        if overhead_pool:
            self._allocate_overhead(Decimal(str(overhead_pool)))
        return dict(self.stats)

    # -- lot resolution with a per-run cache (Lot.code is a property; scans are O(n))
    def _lot(self, code, required=True):
        if code in self._lot_cache:
            return self._lot_cache[code]
        lot = find_lot(code)
        if lot is None and required:
            raise ValueError(f"lot '{code}' not found at write time")
        if lot is not None:
            self._lot_cache[code] = lot
        return lot

    def _make_lot(self, code, abbr, vintage, variety, program, status, intent=""):
        lot = self._lot(code, required=False)
        if lot is not None:
            return lot
        lot = generator.create_lot(vintage, variety, program, status=status,
                                   production_intent=intent, override_code=abbr)
        if lot.code != code:
            raise ValueError(
                f"lot code round-trip failed: asked for '{code}', got '{lot.code}'. "
                f"The designation renderer and the CSV disagree — do not import on top "
                f"of this.")
        self._lot_cache[code] = lot
        self.stats["lots"] += 1
        return lot

    def _write_opening(self):
        for r in self.parsed.get("00_opening_inventory.csv", []):
            lot = self._make_lot(r["lot_code"], r["abbr"], r["vintage_year"], r["variety"],
                                 r["program"], Lot.Status.DONE_PRIMARY,
                                 intent="carry-in opening inventory (historical import)")
            if lot.bond_bookings.filter(voided_at__isnull=True).exists():
                continue
            vm = VolumeMeasurement.objects.create(
                lot=lot, method=VolumeMeasurement.Method.STATED,
                measured_at=aware(r["as_of"]),
                volume_gal=r["gallons"], is_booking_volume=True,
                notes="opening inventory — historical import")
            BookToBond.objects.create(
                lot=lot, booked_at=r["as_of"], gallons_produced=r["gallons"],
                tax_class=r["tax_class"], gauge_source=BookToBond.GaugeSource.STATED,
                volume=vm,
                notes=f"carry-in opening inventory. {r['notes']}".strip())
            self.stats["opening_lots"] += 1
            if r["cost_basis"]:
                LotCostAdjustment.objects.create(
                    lot=lot, kind=LotCostAdjustment.Kind.OTHER, amount=r["cost_basis"],
                    incurred_at=r["as_of"], basis=LotCostAdjustment.Basis.ENTERED,
                    notes="carry-in cost basis (historical import)")
                self.stats["cost_rows"] += 1

    def _write_spirits(self):
        for r in sorted(self.parsed.get("01_spirit_receipts.csv", []),
                        key=lambda x: x["event_date"]):
            dup = HighProofSpiritLedger.objects.filter(
                event_type=HighProofSpiritLedger.EventType.RECEIPT,
                event_date=r["event_date"], wine_gallons=r["wine_gallons"],
                proof=r["proof"], voided_at__isnull=True).exists()
            if dup:
                continue
            HighProofSpiritLedger.objects.create(
                event_type=HighProofSpiritLedger.EventType.RECEIPT,
                event_date=r["event_date"], wine_gallons=r["wine_gallons"],
                proof=r["proof"], proof_gallons=r["proof_gallons"],
                supplier=r["supplier"], shipment_ref=r["shipment_ref"],
                cost=r["cost"], notes=r["notes"])
            self.stats["spirit_receipts"] += 1

    def _resolve_block(self, r):
        """HarvestEvent requires a Block, so every weigh tag needs one.

        Prefer the curated master data: match the vineyard and block by name and use
        what is already there. Auto-creation is the fallback, not the default, and it
        is WARNED about — a Block carries a variety and participates in
        VarietalDesignation resolution (block > vineyard > variety), so quietly
        minting "Mohr-Fry Ranches / 23 Rows" a second time under a slightly different
        spelling would fork the abbreviation catalog.
        """
        from cellar.models import Block, Grower, Vineyard

        vname = r["vineyard_name"] or "Historical (unspecified)"
        bname = r["block_name"] or "—"

        vy = Vineyard.objects.filter(name__iexact=vname).first()
        if vy is None:
            grower, _ = Grower.objects.get_or_create(
                name=vname, defaults={"source_type": r["source_type"]})
            vy = Vineyard.objects.create(grower=grower, name=vname)
            self.warnings.append(
                f"created Vineyard '{vname}' (+ Grower) — not in your master data. "
                f"Check the spelling against the curated list.")

        block = Block.objects.filter(vineyard=vy, name__iexact=bname).first()
        if block is None:
            block = Block.objects.create(vineyard=vy, name=bname, variety=r["variety"])
            self.warnings.append(
                f"created Block '{vname} / {bname}' ({r['variety'].name}) — not in "
                f"your master data. Verify before it feeds designation resolution.")
        return block

    def _write_fruit(self):
        for r in sorted(self.parsed.get("02_fruit.csv", []), key=lambda x: x["harvest_date"]):
            lot = self._make_lot(r["lot_code"], r["abbr"], r["vintage_year"], r["variety"],
                                 r["program"], Lot.Status.DONE_PRIMARY,
                                 intent="historical import")
            block = self._resolve_block(r)
            he, _ = HarvestEvent.objects.get_or_create(block=block,
                                                       harvest_date=r["harvest_date"])
            tagno = r["weigh_tag_number"] or f"HIST-{r['lot_code']}-{r['harvest_date']:%m%d}"
            tag = WeighTag.objects.filter(weigh_tag_number=tagno).first()
            if tag is None:
                tag = WeighTag.objects.create(
                    weigh_tag_number=tagno, harvest_event=he,
                    source_type=r["source_type"],
                    disposition=WeighTag.Disposition.CRUSHED,
                    net_weight_lbs=r["net_lbs"], brix_at_receipt=r["brix"],
                    purchase_price_per_ton=r["purchase_price_per_ton"],
                    fruit_cost_per_ton=r["fruit_cost_per_ton"],
                    locked=True, notes=r["notes"])
                self.stats["weigh_tags"] += 1
            if not WeighTagAllocation.objects.filter(
                    weigh_tag=tag, lot=lot, voided_at__isnull=True).exists():
                WeighTagAllocation.objects.create(
                    weigh_tag=tag, lot=lot, allocated_net_lbs=r["net_lbs"],
                    notes="historical import")
                self.stats["allocations"] += 1

    def _write_production(self):
        for r in sorted(self.parsed.get("03_production.csv", []), key=lambda x: x["booked_at"]):
            lot = self._lot(r["lot_code"])
            method = r["method"]

            # Booked before the production branch below, which may `continue` past a
            # fortification that already exists.
            for kind, amt in (("oak", r["oak_cost"]), ("other", r["other_cost"])):
                if not amt:
                    continue
                k = (LotCostAdjustment.Kind.OAK if kind == "oak"
                     else LotCostAdjustment.Kind.OTHER)
                if LotCostAdjustment.objects.filter(lot=lot, kind=k,
                                                    incurred_at=r["booked_at"],
                                                    voided_at__isnull=True).exists():
                    continue
                LotCostAdjustment.objects.create(
                    lot=lot, kind=k, amount=amt, incurred_at=r["booked_at"],
                    basis=LotCostAdjustment.Basis.ENTERED,
                    notes="historical import")
                self.stats["cost_rows"] += 1

            if method == "book_to_bond":
                if not lot.bond_bookings.filter(voided_at__isnull=True).exists():
                    vm = VolumeMeasurement.objects.create(
                        lot=lot, method=VolumeMeasurement.Method.STATED,
                        measured_at=aware(r["booked_at"]),
                        volume_gal=r["gallons"], is_booking_volume=True,
                        notes="historical import")
                    BookToBond.objects.create(
                        lot=lot, booked_at=r["booked_at"], gallons_produced=r["gallons"],
                        tax_class=r["tax_class"],
                        gauge_source=BookToBond.GaugeSource.STATED, volume=vm,
                        notes=r["notes"] or "historical import")
                    self.stats["book_to_bond"] += 1
            elif method == "fortify_initial":
                # Idempotency matters more here than anywhere else in the importer:
                # FortificationEvent.save() posts a DRAW against the HPGS account, so a
                # second run does not merely duplicate a row, it double-spends the
                # spirit inventory and then fails the next lot for insufficient balance.
                if lot.fortifications.filter(
                        kind=FortificationEvent.Kind.INITIAL, booked_at=r["booked_at"],
                        proof_gallons_drawn=r["pg_drawn"], voided_at__isnull=True).exists():
                    self.stats["fortifications_skipped"] += 1
                    continue
                fort_svc.fortify(
                    lot,
                    fortified_on_skins_date=r["on_skins_date"] or r["booked_at"],
                    booked_at=r["booked_at"],
                    proof_gallons_drawn=r["pg_drawn"],
                    finished_wg=r["gallons"],
                    spirit_proof=r["spirit_proof"],
                    expected_tax_class=r["tax_class"])
                self.stats["fortifications"] += 1
            else:
                if lot.fortifications.filter(
                        kind=FortificationEvent.Kind.ADJUSTMENT, booked_at=r["booked_at"],
                        proof_gallons_drawn=r["pg_drawn"], voided_at__isnull=True).exists():
                    self.stats["fortifications_skipped"] += 1
                    continue
                fort_svc.adjust_alcohol(
                    lot, adjusted_at=r["booked_at"],
                    proof_gallons_drawn=r["pg_drawn"],
                    base_wg=r["base_wg"], finished_wg=r["gallons"],
                    spirit_proof=r["spirit_proof"],
                    base_tax_class=r["tax_class"],
                    expected_tax_class=r["tax_class"])
                self.stats["fortifications"] += 1

    def _write_blends(self):
        for r in sorted(self.parsed.get("04_blends.csv", []), key=lambda x: x["blended_at"]):
            child = self._lot(r["child_code"], required=False)
            if child is None:
                if r["variety"] is None:
                    raise ValueError(
                        f"blend '{r['child_code']}' is a new lot and needs a variety "
                        f"column so its designation can be built")
                child = self._make_lot(r["child_code"], r["abbr"], r["vintage_year"],
                                       r["variety"], r["program"],
                                       Lot.Status.DONE_PRIMARY,
                                       intent="blend (historical import)")
            total = sum(p["gallons"] for p in r["parents"])
            gallons = r["gallons"] or total
            has_gauge = (child.volume_measurements.filter(voided_at__isnull=True).exists()
                         or child.bond_bookings.filter(voided_at__isnull=True).exists()
                         or child.fortifications.filter(voided_at__isnull=True).exists())
            if not has_gauge:
                # The blend's volume is the gauge of record for the child. No BookToBond:
                # blending does not PRODUCE wine for 5120.17 purposes — it moves wine that
                # is already in bond. A stated VolumeMeasurement is what costing and
                # composition read, and it is enough.
                VolumeMeasurement.objects.create(
                    lot=child, method=VolumeMeasurement.Method.STATED,
                    measured_at=aware(r["blended_at"]),
                    volume_gal=gallons, is_booking_volume=True,
                    notes="blend volume — historical import")
                self.stats["blend_volumes"] += 1
            for p in r["parents"]:
                parent = self._lot(p["code"])
                if LotLineage.objects.filter(parent_lot=parent, child_lot=child,
                                             relationship_type=r["relationship"],
                                             voided_at__isnull=True).exists():
                    continue
                # Rows are processed in blended_at order, so by the time an edge is
                # written the parent already carries every cost booked before that
                # blend — the snapshot is right without a second pass.
                from cellar.services import costing as costing_svc
                cpg = costing_svc.parent_cost_per_gal(parent)
                LotLineage.objects.create(
                    parent_lot=parent, child_lot=child,
                    relationship_type=r["relationship"],
                    volume_gal=p["gallons"],
                    occurred_at=costing_svc.to_business_date(r["blended_at"]),
                    cost_per_gal_snapshot=cpg,
                    notes=f"{r['blended_at']} historical import. {r['notes']}".strip())
                self.stats["lineage_edges"] += 1

    def _write_bottling(self):
        for r in sorted(self.parsed.get("05_bottling.csv", []), key=lambda x: x["bottled_at"]):
            lot = self._lot(r["lot_code"])
            run = BottlingRun.objects.filter(source_lot=lot, sku=r["sku"],
                                             bottled_at=r["bottled_at"],
                                             voided_at__isnull=True).first()
            if run is None:
                run = BottlingRun.objects.create(
                    source_lot=lot, bottle_format=r["format"], sku=r["sku"],
                    bottled_at=r["bottled_at"], bulk_gallons_in=r["bulk_gallons_in"],
                    cases_produced=r["cases"], line_labor_cost=r["line_labor_cost"],
                    notes=r["notes"] or "historical import")
                self.stats["bottling_runs"] += 1
            for d in r["dry_goods"]:
                if BottlingDryGoodUse.objects.filter(run=run, dry_good=d["dg"],
                                                     voided_at__isnull=True).exists():
                    continue
                BottlingDryGoodUse.objects.create(run=run, dry_good=d["dg"], quantity=d["qty"])
                self.stats["dry_good_uses"] += 1

    def _write_removals(self):
        for r in sorted(self.parsed.get("06_removals.csv", []), key=lambda x: x["occurred_at"]):
            kind = r["kind"]
            dest = get_destination(r["destination"])
            if kind == "taxpaid_bottled":
                run = BottlingRun.objects.filter(sku=r["sku"], bottled_at=r["run_bottled_at"],
                                                 voided_at__isnull=True).first()
                if run is None:
                    raise ValueError(
                        f"no bottling run for sku '{r['sku']}' dated {r['run_bottled_at']} "
                        f"— referenced by a removal on {r['occurred_at']}")
                if TaxPaidRemoval.objects.filter(bottling_run=run, removed_at=r["occurred_at"],
                                                 cases=r["cases"], channel=r["channel"],
                                                 voided_at__isnull=True).exists():
                    continue
                TaxPaidRemoval.objects.create(
                    bottling_run=run, removed_at=r["occurred_at"], cases=r["cases"],
                    channel=r["channel"], notes=r["notes"] or "historical import")
                self.stats["taxpaid_removals"] += 1
            elif kind == "taxpaid_bulk":
                if BulkTaxPaidRemoval.objects.filter(
                        lot=self._lot(r["lot_code"]), removed_at=r["occurred_at"],
                        wine_gallons=r["gallons"], voided_at__isnull=True).exists():
                    continue
                BulkTaxPaidRemoval.objects.create(
                    lot=self._lot(r["lot_code"]), tax_class=r["tax_class"],
                    wine_gallons=r["gallons"], removed_at=r["occurred_at"],
                    channel=r["channel"], destination=dest,
                    notes=r["notes"] or "historical import")
                self.stats["bulk_removals"] += 1
            elif kind in ("bond_transfer_out", "bond_transfer_in"):
                if BondTransfer.objects.filter(
                        transferred_at=r["occurred_at"], gallons=r["gallons"],
                        counterparty=r["destination"], voided_at__isnull=True).exists():
                    continue
                BondTransfer.objects.create(
                    direction=(BondTransfer.Direction.OUT if kind.endswith("out")
                               else BondTransfer.Direction.IN),
                    tax_class=r["tax_class"], gallons=r["gallons"],
                    transferred_at=r["occurred_at"], counterparty=r["destination"],
                    destination=dest,
                    lot=self._lot(r["lot_code"], required=False) if r["lot_code"] else None,
                    notes=r["notes"] or "historical import")
                self.stats["bond_transfers"] += 1
            elif kind == "must_sale":
                if MustSale.objects.filter(
                        lot=self._lot(r["lot_code"]), sold_at=r["occurred_at"],
                        gallons=r["gallons"], voided_at__isnull=True).exists():
                    continue
                MustSale.objects.create(
                    lot=self._lot(r["lot_code"]), gallons=r["gallons"],
                    sold_at=r["occurred_at"], price_per_gallon=r["price_per_gallon"],
                    destination=dest, notes=r["notes"] or "historical import")
                self.stats["must_sales"] += 1
            else:
                if BondAdjustment.objects.filter(
                        kind=r["adjustment_kind"], occurred_at=r["occurred_at"],
                        gallons=r["gallons"], voided_at__isnull=True).exists():
                    continue
                BondAdjustment.objects.create(
                    kind=r["adjustment_kind"], gallons=r["gallons"],
                    tax_class=r["tax_class"], occurred_at=r["occurred_at"],
                    lot=self._lot(r["lot_code"], required=False) if r["lot_code"] else None,
                    notes=r["notes"] or "historical import")
                self.stats["adjustments"] += 1

    # ------------------------------------------------------------ overhead pool
    def _allocate_overhead(self, pool):
        """Spread a single dollar pool across every lot this import created, by
        gallons produced. Volume-weighted rather than per-lot flat, because a
        3,000-gallon lot did not consume the same cellar overhead as a 200-gallon one.

        Booked as basis=ALLOCATED so an auditor can tell it from an entered figure.
        """
        from cellar.services.aging import _lot_volume

        codes = {r["lot_code"] for r in self.parsed.get("03_production.csv", [])}
        vols = {}
        for code in codes:
            lot = self._lot(code, required=False)
            if lot is None:
                continue
            # _lot_volume(), not booking_volume_for(): a fortified lot has no
            # VolumeMeasurement of its own — its volume lives on the
            # FortificationEvent — and skipping it here would hand its share of
            # the overhead pool to the table wines.
            v = _lot_volume(lot)
            if v:
                vols[lot] = Decimal(str(v))
        total = sum(vols.values())
        if not total:
            self.warnings.append("overhead pool not allocated — no lot volumes found")
            return
        as_of = max(r["booked_at"] for r in self.parsed.get("03_production.csv", []))
        running = Decimal("0")
        items = sorted(vols.items(), key=lambda kv: kv[0].id)
        for i, (lot, v) in enumerate(items):
            if i == len(items) - 1:
                amt = (pool - running).quantize(CENT)      # last lot absorbs the rounding
            else:
                amt = (pool * v / total).quantize(CENT)
                running += amt
            if LotCostAdjustment.objects.filter(
                    lot=lot, kind=LotCostAdjustment.Kind.OVERHEAD,
                    basis=LotCostAdjustment.Basis.ALLOCATED,
                    voided_at__isnull=True).exists():
                continue
            LotCostAdjustment.objects.create(
                lot=lot, kind=LotCostAdjustment.Kind.OVERHEAD, amount=amt,
                incurred_at=as_of, basis=LotCostAdjustment.Basis.ALLOCATED,
                notes=f"allocated from ${pool} pool by volume ({v} gal of {total})")
            self.stats["overhead_rows"] += 1
