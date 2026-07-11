"""
Lot ID generator, ORM edition — the standalone spec wired to the database.

Two things change from the reference implementation:
  * resolve() reads the VarietalDesignation table (block > vineyard > variety).
  * next sequence comes from a row-locked LotSequenceCounter, so two people
    creating a lot at the same moment during crush can't collide. The counter
    is monotonic → numbers are never reused.
"""
import re
import unicodedata
from django.db import transaction
from django.utils import timezone

from cellar.models import (
    Program, LotKind, VarietalDesignation, LotSequenceCounter,
    Lot, LotDesignation,
)


# --------------------------------------------------------------------- resolve
def _stem(variety) -> str:
    """Provisional abbreviation when the curated catalog has no entry.

    Multi-word varieties take their initials — which is exactly the house
    convention (Touriga Nacional -> TN, Tinta Cao -> TC). Single-word varieties
    take their first four letters (Verdelho -> VERD, Trousseau -> TROU,
    Tempranillo -> TEMP), because initials collapse them to one letter and collide
    (Trousseau and Tempranillo would both be "T").

    Diacritics are folded first, so Souzao -> SOUZ. Still flagged provisional by
    the caller — curate it in the VarietalDesignation catalog to make it official.
    """
    folded = unicodedata.normalize("NFKD", variety.name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    words = [w for w in re.split(r"[^A-Za-z]+", folded) if w]
    if not words:
        return "LOT"
    if len(words) > 1:
        return "".join(w[0] for w in words)[:4].upper()
    return words[0][:4].upper()


def resolve_abbreviation(variety, program, block=None, vineyard=None):
    """Returns (abbreviation, is_provisional). Never raises — unknown combos
    autofire a provisional code flagged for review."""
    qs = VarietalDesignation.objects.filter(variety=variety, program=program)

    if block is not None:
        hit = qs.filter(block=block).first()
        if hit:
            return hit.abbreviation, not hit.is_curated
    if vineyard is not None:
        hit = qs.filter(vineyard=vineyard).first()
        if hit:
            return hit.abbreviation, not hit.is_curated
    hit = qs.filter(block__isnull=True, vineyard__isnull=True).first()
    if hit:
        return hit.abbreviation, not hit.is_curated

    # port fallback: <table code> + PORT, suggested for confirmation
    if program == Program.PORT:
        table_abbr, _ = resolve_abbreviation(variety, Program.TABLE, block, vineyard)
        if table_abbr:
            return table_abbr + "PORT", True
        # If no table_abbr, fall through to generate provisional PORT code
        return f"{_stem(variety)}PORT", True

    # BASE CASE: Don't recurse if we're already at TABLE level
    if program == Program.TABLE:
        # no code at all → provisional placeholder, flagged
        return _stem(variety), True

    # RECURSIVE CASE: Fall back to TABLE level
    table_abbr, _ = resolve_abbreviation(variety, Program.TABLE, block, vineyard)
    stem = table_abbr or _stem(variety)
    suffix = {"rose": "ROSE", "port": "PORT", "table": ""}[program]
    return f"{stem}{suffix}", True


# -------------------------------------------------------------------- sequence
def next_sequence(vintage, abbreviation):
    """Atomic, monotonic. Must run inside a transaction (create_lot wraps it)."""
    counter, _ = LotSequenceCounter.objects.select_for_update().get_or_create(
        vintage=vintage, abbreviation=abbreviation)
    counter.last_seq += 1
    counter.save()
    return counter.last_seq


def _abbr_lot_count(vintage, abbreviation):
    """How many current single-member lots share this abbreviation this vintage
    (drives the singleton display rule)."""
    n = 0
    for d in LotDesignation.objects.filter(effective_to__isnull=True, kind=LotKind.STANDARD):
        if d.lot.vintage_year == vintage and len(d.members) == 1 \
                and d.members[0]["abbr"] == abbreviation:
            n += 1
    return n


# ---------------------------------------------------------------------- render
def render_designation(d):
    """Render a LotDesignation's components to its display string."""
    vv = f"{d.lot.vintage_year % 100:02d}"
    members = d.members

    if len(members) == 1:
        m = members[0]
        core = m["abbr"]
        if m.get("seq") is not None:
            lone = (m["seq"] == 1 and _abbr_lot_count(d.lot.vintage_year, m["abbr"]) <= 1)
            if not lone:
                core += str(m["seq"])
        s = f"{vv}{core}"
        if d.custom_suffix:
            s += f"_{d.custom_suffix}"
        return s

    abbrs = {m["abbr"] for m in members}
    if len(abbrs) == 1:  # same-variety blend → 24TEMP4/5/6
        seqs = "/".join(str(m["seq"]) for m in members)
        core = f"{vv}{members[0]['abbr']}{seqs}"
    else:
        # differing → full member codes joined "/"  (25SOUZPORT1/TNPORT1, 24TCPORT/SOUZPORT)
        parts = [m["abbr"] + (str(m["seq"]) if m.get("seq") is not None else "") for m in members]
        core = f"{vv}" + "/".join(parts)
    # suffix applies to blends too — a bottling parcel split off a blend would otherwise
    # drop its _B1 and render identically to its parent.
    if d.custom_suffix:
        core += f"_{d.custom_suffix}"
    return core


# ------------------------------------------------------------- creation points
@transaction.atomic
def assign_initial_designation(lot, variety, program, block=None, vineyard=None,
                               override_code=None):
    """Attach an initial code to an already-created Lot (used by the admin, which
    creates the Lot row itself, then calls this)."""
    if override_code:
        d = LotDesignation.objects.create(
            lot=lot, kind=LotKind.STANDARD,
            members=[{"abbr": override_code, "seq": None}])
    else:
        abbr, provisional = resolve_abbreviation(variety, program, block, vineyard)
        seq = next_sequence(lot.vintage_year, abbr)
        d = LotDesignation.objects.create(
            lot=lot, kind=LotKind.STANDARD,
            members=[{"abbr": abbr, "seq": seq}], is_provisional=provisional)
    lot.current_designation = d
    lot.save(update_fields=["current_designation"])
    return d


@transaction.atomic
def create_lot(vintage, variety, program, block=None, vineyard=None,
               status=Lot.Status.RECEIVING, production_intent="", override_code=None):
    """Fresh single-variety crush. Autofires the code; override_code wins if given."""
    lot = Lot.objects.create(vintage_year=vintage, status=status,
                             production_intent=production_intent)
    assign_initial_designation(lot, variety, program, block, vineyard,
                               override_code=override_code)
    return lot


@transaction.atomic
def redesignate(lot, variety, program, block=None, vineyard=None):
    """Whole-lot program change: close the current designation, open the next code."""
    current = lot.current_designation
    if current:
        current.effective_to = timezone.now()
        current.save(update_fields=["effective_to"])
    abbr, provisional = resolve_abbreviation(variety, program, block, vineyard)
    seq = next_sequence(lot.vintage_year, abbr)
    d = LotDesignation.objects.create(
        lot=lot, kind=LotKind.STANDARD, reason=LotDesignation.Reason.REDESIGNATION,
        members=[{"abbr": abbr, "seq": seq}], is_provisional=provisional)
    lot.current_designation = d
    lot.save(update_fields=["current_designation"])
    return lot


# ------------------------------------------------------- bottling parcels
def next_parcel_suffix(parent) -> str:
    """B1, B2, ... — the next bottling-parcel suffix for this parent lot.

    Numbered per parent, so bottling the same wine twice in a vintage gives
    25VERD_B1 and 25VERD_B2 rather than a collision.
    """
    n = LotDesignation.objects.filter(
        lot__lineage_as_child__parent_lot=parent,
        lot__lineage_as_child__voided_at__isnull=True,
        kind=LotKind.BOTTLING, effective_to__isnull=True).count()
    return f"B{n + 1}"


@transaction.atomic
def assign_parcel_designation(child, parent, suffix=None):
    """Code a bottling parcel off its parent: same members, plus a _B<n> suffix.

    The parent's abbreviation and sequence are REUSED verbatim (no new sequence is
    drawn) so the parcel reads as what it is — a piece of 25VERD, not a new lot in
    its own right. kind=BOTTLING keeps it out of _abbr_lot_count(), so the parent's
    own code is unaffected by the split.
    """
    pd = parent.current_designation
    if pd is None:
        raise ValueError(f"{parent} has no designation to split from.")
    suffix = suffix or next_parcel_suffix(parent)

    d = LotDesignation.objects.create(
        lot=child, kind=LotKind.BOTTLING, members=list(pd.members),
        custom_suffix=suffix, is_provisional=pd.is_provisional)
    child.current_designation = d
    child.save(update_fields=["current_designation"])
    return d
