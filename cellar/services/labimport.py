"""
ETS lab-report CSV import.

An ETS export is one row per analysis in long form; a physical sample (one
'Sample #') carries a whole panel across many rows. This service:

  1. parses the CSV (long form, CRLF, ETS column names);
  2. groups rows into samples by 'Sample #';
  3. auto-matches each sample's 'Sample Description' to a Lot by computed code
     (Nate: the description always equals a lot code) — a single match commits,
     zero or many stops as unresolved/ambiguous;
  4. parses each raw Result into value / qualifier / flag / display, applying the
     censoring rules from the cleaned-data Read Me (< → ND or Dry, > on stability
     → FAIL, etc.);
  5. maps each 'Analysis Name' to a canonical LabAnalyte via synonym then name;
  6. classifies the sample's panel (juice vs chemistry vs …);
  7. commits idempotently — dedupe key (source=ETS, sample_id, analyte), so
     re-uploading the same report adds nothing.

`plan(text)` reads only (matching + dup checks); `commit(text, user)` writes.
The web layer previews with plan(), then commits the same CSV text.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from cellar.models import (Lot, LabAnalyte, LabAnalyteSynonym, LabResult,
                           LabResultValue, LabSampleAlias)
from cellar.services import labpanels

# ETS header names we rely on
COL_DATE = "Date"
COL_DESC = "Sample Description"
COL_GROUP = "Group #"
COL_SAMPLE = "Sample #"
COL_ANALYSIS = "Analysis Name"
COL_RESULT = "Result"
COL_UNITS = "Units"

# '<' censoring: which analyte reads as "Dry" vs "ND".
_DRY_SLUGS = {"glucose_fructose"}
_STABILITY_SLUGS = {"heat_stability", "turbidity"}


# --------------------------------------------------------------------- parsing
def _clean(s):
    return (s or "").replace("\ufeff", "").strip()


def _parse_dt(raw):
    raw = _clean(raw)
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
        except ValueError:
            continue
    return None


def parse_result(raw, slug):
    """Turn a raw ETS Result string into (value, qualifier, flag, display).

    value    numeric reading the calcs use (0 for ND / Dry)
    qualifier one of '=', '<', '>'
    flag     '' | ND | Dry | Pass | FAIL | note
    display  what the report shows (ND / Dry / FAIL / Pass / the number / text)
    """
    Q = LabResultValue.Qualifier
    F = LabResultValue.Flag
    raw = _clean(raw)

    def _num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    if raw.startswith("<"):
        rest = _num(raw[1:].strip())
        if slug in _STABILITY_SLUGS:                     # heat-stable
            return (rest or 0.0), Q.LT, F.PASS, "Pass"
        if slug in _DRY_SLUGS:                            # sugar undetectable
            return 0.0, Q.LT, F.DRY, "Dry"
        return 0.0, Q.LT, F.ND, "ND"                     # below detection

    if raw.startswith(">"):
        rest = _num(raw[1:].strip())
        if slug in _STABILITY_SLUGS:                     # heat-unstable
            return (rest or 0.0), Q.GT, F.FAIL, "FAIL"
        return (rest or 0.0), Q.GT, F.NONE, raw

    n = _num(raw)
    if n is not None:
        # strip trailing .0 for a clean display but keep the value numeric
        disp = ("%g" % n)
        return n, Q.EQ, F.NONE, disp

    # free-text (e.g. a heat-stability trial description) — keep as a note
    return 0.0, Q.EQ, F.NOTE, (raw[:40] or "note")


# ------------------------------------------------------------- analyte mapping
def _analyte_index():
    """(synonym map lower->analyte, name map lower->analyte)."""
    syn = {s.raw_name.lower(): s.analyte
           for s in LabAnalyteSynonym.objects.select_related("analyte")}
    names = {a.name.lower(): a for a in LabAnalyte.objects.all()}
    return syn, names


def _match_analyte(name, syn, names):
    key = _clean(name).lower()
    return syn.get(key) or names.get(key)


# --------------------------------------------------------------- lot matching
def _lot_index_for_vintages(vintages):
    """Map computed lot code -> [lots] for the given 2-digit vintages."""
    idx = {}
    qs = Lot.objects.filter(voided_at__isnull=True) if hasattr(Lot, "voided_at") else Lot.objects.all()
    for lot in qs.select_related("current_designation__lot"):
        if (lot.vintage_year % 100) not in vintages:
            continue
        code = lot.code
        idx.setdefault(code, []).append(lot)
    return idx


def _alias_index():
    """Manually-bound description -> Lot. Consulted BEFORE the computed-code match,
    so a binding always wins over (and can override) code inference."""
    return {a.description.lower(): a.lot
            for a in LabSampleAlias.objects.select_related("lot__current_designation")}


def _desc_vintage(desc):
    d = _clean(desc)
    return int(d[:2]) if len(d) >= 2 and d[:2].isdigit() else None


# ------------------------------------------------------------------ plan types
@dataclass
class ValueRow:
    analysis: str
    slug: str | None
    analyte_name: str | None
    unit: str
    display: str
    value: float
    qualifier: str
    flag: str
    raw: str
    dup: bool = False          # already imported (idempotency)


@dataclass
class SampleGroup:
    description: str
    sample_id: str
    group_id: str
    reported_at: datetime | None
    panel: str = LabResult.Panel.OTHER
    lot: object | None = None
    lot_code: str = ""
    status: str = "matched"    # matched | ambiguous | unresolved
    values: list = field(default_factory=list)
    unknown: list = field(default_factory=list)   # analysis names with no analyte

    @property
    def new_count(self):
        return sum(1 for v in self.values if not v.dup)

    @property
    def panel_display(self):
        return LabResult.Panel(self.panel).label


@dataclass
class ImportPlan:
    samples: list = field(default_factory=list)
    error: str = ""

    @property
    def matched(self):
        return [s for s in self.samples if s.status == "matched"]

    @property
    def unresolved(self):
        return [s for s in self.samples if s.status != "matched"]

    @property
    def total_new_values(self):
        return sum(s.new_count for s in self.matched)

    @property
    def total_dupes(self):
        return sum(sum(1 for v in s.values if v.dup) for s in self.matched)

    @property
    def total_unknown(self):
        return sum(len(s.unknown) for s in self.samples)


# ------------------------------------------------------------------- planning
def _read_rows(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    missing = {COL_DESC, COL_SAMPLE, COL_ANALYSIS, COL_RESULT} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing expected ETS columns: {', '.join(sorted(missing))}")
    return list(reader)


def plan(text) -> ImportPlan:
    """Parse + match + dup-check without writing. Safe to call repeatedly."""
    try:
        rows = _read_rows(text)
    except Exception as e:  # noqa: BLE001
        return ImportPlan(error=str(e))

    syn, names = _analyte_index()
    alias_idx = _alias_index()

    # group rows by Sample #
    groups: dict[str, list] = {}
    order: list[str] = []
    for r in rows:
        sid = _clean(r.get(COL_SAMPLE))
        if sid not in groups:
            groups[sid] = []
            order.append(sid)
        groups[sid].append(r)

    # lot index for the vintages present
    vintages = set()
    for sid in order:
        v = _desc_vintage(groups[sid][0].get(COL_DESC))
        if v is not None:
            vintages.add(v)
    lot_idx = _lot_index_for_vintages(vintages) if vintages else {}

    samples = []
    for sid in order:
        grp = groups[sid]
        desc = _clean(grp[0].get(COL_DESC))
        gid = _clean(grp[0].get(COL_GROUP))
        dates = [d for d in (_parse_dt(r.get(COL_DATE)) for r in grp) if d]
        reported = max(dates) if dates else timezone.now()

        sg = SampleGroup(description=desc, sample_id=sid, group_id=gid, reported_at=reported)

        # resolve lot — a manual binding wins, then the computed code
        alias = alias_idx.get(desc.lower())
        candidates = [alias] if alias is not None else lot_idx.get(desc, [])
        if len(candidates) == 1:
            sg.lot = candidates[0]
            sg.lot_code = candidates[0].code
            sg.status = "matched"
        elif len(candidates) > 1:
            sg.status = "ambiguous"
        else:
            sg.status = "unresolved"

        # parse each analysis row
        slugs = []
        for r in grp:
            aname = _clean(r.get(COL_ANALYSIS))
            analyte = _match_analyte(aname, syn, names)
            if analyte is None:
                sg.unknown.append(aname)
                continue
            slugs.append(analyte.slug)
            val, qual, flag, disp = parse_result(r.get(COL_RESULT), analyte.slug)
            sg.values.append(ValueRow(
                analysis=aname, slug=analyte.slug, analyte_name=analyte.name,
                unit=analyte.unit or _clean(r.get(COL_UNITS)),
                display=disp, value=val, qualifier=qual, flag=flag,
                raw=_clean(r.get(COL_RESULT)),
            ))

        sg.panel = labpanels.classify(slugs)

        # dup check against already-imported values for this sample
        if sg.lot is not None:
            existing = LabResult.objects.filter(
                lot=sg.lot, source=LabResult.Source.ETS, sample_id=sid,
                voided_at__isnull=True).first()
            if existing:
                have = {v.analyte.slug for v in existing.values.all()}
                for v in sg.values:
                    if v.slug in have:
                        v.dup = True

        samples.append(sg)

    return ImportPlan(samples=samples)


# -------------------------------------------------------------------- commit
@transaction.atomic
def bind_samples(binds):
    """Persist description -> lot bindings chosen in the preview. `binds` is
    {description: lot_pk}. Upserts, so re-binding corrects a mistake."""
    made = 0
    for desc, lot_pk in (binds or {}).items():
        if not desc or not lot_pk:
            continue
        lot = Lot.objects.filter(pk=lot_pk).first()
        if lot is None:
            continue
        LabSampleAlias.objects.update_or_create(description=desc, defaults={"lot": lot})
        made += 1
    return made


def commit(text, user=None, binds=None):
    """Apply a plan. Only matched samples with new values are written; dupes and
    unresolved samples are skipped. Returns (results_touched, values_written).

    `binds` ({description: lot_pk}) is saved FIRST, so samples the user just bound
    in the preview resolve on this very commit."""
    if binds:
        bind_samples(binds)
    p = plan(text)
    if p.error:
        raise ValueError(p.error)

    op = user if (user and getattr(user, "is_authenticated", False)) else None
    results_touched = 0
    values_written = 0

    for sg in p.matched:
        new_vals = [v for v in sg.values if not v.dup]
        if not new_vals:
            continue

        result = LabResult.objects.filter(
            lot=sg.lot, source=LabResult.Source.ETS, sample_id=sg.sample_id,
            voided_at__isnull=True).first()
        if result is None:
            result = LabResult.objects.create(
                lot=sg.lot, reported_at=sg.reported_at, source=LabResult.Source.ETS,
                panel=sg.panel, sample_id=sg.sample_id, operator=op)
        results_touched += 1

        by_slug = {a.slug: a for a in LabAnalyte.objects.filter(
            slug__in=[v.slug for v in new_vals])}
        for v in new_vals:
            LabResultValue.objects.create(
                result=result, analyte=by_slug[v.slug],
                value=round(v.value, 3), qualifier=v.qualifier, flag=v.flag,
                display=v.display, raw_result=v.raw, operator=op)
            values_written += 1

    return results_touched, values_written
