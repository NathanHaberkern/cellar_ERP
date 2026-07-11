"""
Lab-panel definitions and classification — the single source of truth for what a
"Juice panel" and a "Chemistry panel" contain, whether a given result is a *full*
panel, and which full panel is the most recent for a lot.

Rules (locked with Nate):
  * A sample is a JUICE panel iff it carries a Brix reading (harvest juice);
    otherwise, if it carries any chemistry-only analyte, it's a CHEMISTRY panel
    (spring racking). Heat-stability and smoke sit apart.
  * "Full" = the panel is missing at most one of its canonical analytes
    (all-but-one still counts).

Membership is by analyte slug, so a display-name change never moves an analyte
between panels. Keep these sets in step with seed_lab_analytes.
"""
from cellar.models import LabResult

JUICE_PANEL = frozenset({
    "brix", "ta", "ph", "glucose_fructose", "va", "l_malic",
    "tartaric", "yan", "ammonia", "amino", "potassium",
})
CHEMISTRY_PANEL = frozenset({
    "fso2", "tso2", "molecular_so2", "ph", "ta", "va",
    "ethanol_20c", "ethanol_60f", "glucose_fructose", "l_malic",
})

# analytes that only appear on the chemistry panel — their presence (absent Brix)
# marks a sample as chemistry rather than a partial juice panel.
_CHEMISTRY_MARKERS = frozenset({"fso2", "tso2", "molecular_so2", "ethanol_20c", "ethanol_60f"})
_SMOKE = frozenset({"guaiacol", "methylguaiacol"})


def classify(slugs):
    """Return the LabResult.Panel value for a set/iterable of analyte slugs."""
    s = set(slugs)
    if "brix" in s:
        return LabResult.Panel.JUICE
    if s & _CHEMISTRY_MARKERS:
        return LabResult.Panel.CHEMISTRY
    if "heat_stability" in s or "turbidity" in s:
        return LabResult.Panel.HEAT_STABILITY
    if s & _SMOKE:
        return LabResult.Panel.SMOKE
    return LabResult.Panel.OTHER


def _canonical_for(panel):
    if panel == LabResult.Panel.JUICE:
        return JUICE_PANEL
    if panel == LabResult.Panel.CHEMISTRY:
        return CHEMISTRY_PANEL
    return None


def is_full(panel, slugs):
    """A juice/chemistry result missing ≤1 canonical analyte is a full panel."""
    canonical = _canonical_for(panel)
    if not canonical:
        return False
    present = canonical & set(slugs)
    return len(present) >= len(canonical) - 1


def result_slugs(result):
    """The analyte slugs present on a saved LabResult (uses prefetched values)."""
    return {v.analyte.slug for v in result.values.all()}


def result_is_full(result):
    return is_full(result.panel, result_slugs(result))


def latest_full_panel(lot):
    """The most recent JUICE-or-CHEMISTRY result for a lot that qualifies as full,
    plus a count of newer partial/other results. Returns (result, newer_partials)
    or (None, 0)."""
    results = list(
        lot.lab_results.filter(voided_at__isnull=True)
        .prefetch_related("values__analyte")
        .order_by("-reported_at", "-id")
    )
    full = None
    newer_partials = 0
    for r in results:
        if r.panel in (LabResult.Panel.JUICE, LabResult.Panel.CHEMISTRY) and result_is_full(r):
            full = r
            break
        newer_partials += 1
    return full, (newer_partials if full else 0)
