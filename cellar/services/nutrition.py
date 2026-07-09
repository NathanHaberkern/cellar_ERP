"""
Yeast nutrition planner (Scott Labs "Develop a Yeast Nutrition Plan").

Framework-agnostic pure logic so it can be unit-tested and dropped into
cellar/services/. No Django imports.

St. Amant only uses Go-Ferm Sterol Flash (rehydration) and Fermaid O, so the
plan is always the "Fermentation Security" column on Fermaid O. The one cell the
planner assigns to Fermaid K (Security / 1/3-depletion / 101-150 ppm) is
substituted with Fermaid O 40 g/hL and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- unit constants -------------------------------------------------------
GAL_PER_HL = 26.417205                      # US gal per hectolitre
# 1 g/hL expressed as lb / 1000 gal (matches the additive sheet's ladder:
# 10 g/hL = 0.83 lb/1000gal, 20 = 1.7, 30 = 2.5, 40 = 3.3)
LB_PER_1000GAL_PER_G_HL = (1000.0 / GAL_PER_HL) / 453.59237  # ~= 0.083454

# --- Table 1: YAN required for fermentation (ppm N) ------------------------
# columns are starting Brix
_BRIX_COLS = (20, 22, 24, 26, 28, 30)
_TABLE1 = {
    "low":    (150, 165, 180, 195, 210, 225),
    "medium": (180, 200, 220, 240, 260, 280),
    "high":   (250, 275, 300, 325, 350, 375),
}

# yeast strain -> nitrogen need (native / uninoculated assumed high)
STRAIN_NEED = {"d21": "medium", "gre": "medium", "native": "high"}

# --- fermentation-security plan on Fermaid O ------------------------------
# band -> list of (stage_key, product, dose g/hL). "K->O" flags the substitution.
_STAGE_2_3_DROP = "2-3_brix_drop"
_STAGE_THIRD = "one_third_sugar_depletion"

_SECURITY_PLAN = {
    "0-50":    [(_STAGE_THIRD, "Fermaid O", 30, False)],
    "51-100":  [(_STAGE_2_3_DROP, "Fermaid O", 20, False),
                (_STAGE_THIRD, "Fermaid O", 40, False)],
    "101-150": [(_STAGE_2_3_DROP, "Fermaid O", 40, False),
                (_STAGE_THIRD, "Fermaid O", 40, True)],   # Fermaid K -> Fermaid O
}

GO_FERM_G_HL = 30  # Go-Ferm Sterol Flash, all fermentations, at rehydration


def resolve_need(strain: str) -> str:
    key = (strain or "").strip().lower()
    if key in STRAIN_NEED:
        return STRAIN_NEED[key]
    if key in ("low", "medium", "high"):
        return key
    raise ValueError(f"Unknown yeast strain / nitrogen need: {strain!r}")


def yan_required(initial_brix: float, need: str) -> float:
    """Table 1 lookup with linear interpolation on Brix; clamps outside 20-30."""
    row = _TABLE1[need]
    b = initial_brix
    if b <= _BRIX_COLS[0]:
        return float(row[0])
    if b >= _BRIX_COLS[-1]:
        return float(row[-1])
    for i in range(len(_BRIX_COLS) - 1):
        lo, hi = _BRIX_COLS[i], _BRIX_COLS[i + 1]
        if lo <= b <= hi:
            frac = (b - lo) / (hi - lo)
            return row[i] + frac * (row[i + 1] - row[i])
    return float(row[-1])  # unreachable


def additional_yan_band(additional: float) -> str:
    if additional <= 50:
        return "0-50"
    if additional <= 100:
        return "51-100"
    return "101-150"


def g_per_hl_to_grams(dose_g_hl: float, volume_gal: float) -> float:
    return dose_g_hl * (volume_gal / GAL_PER_HL)


def g_per_hl_to_lb_per_1000gal(dose_g_hl: float) -> float:
    return dose_g_hl * LB_PER_1000GAL_PER_G_HL


@dataclass
class NutrientAdd:
    stage: str
    product: str
    dose_g_hl: float
    grams: float
    lb_per_1000gal: float
    trigger_brix: float | None   # predicted Brix at which to add; None = at inoculation
    note: str = ""


@dataclass
class NutritionPlan:
    initial_brix: float
    juice_yan: float
    strain: str
    need: str
    volume_gal: float
    yan_required: float
    additional_yan: float
    band: str
    adds: list[NutrientAdd] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_plan(initial_brix: float, juice_yan: float, strain: str,
               volume_gal: float) -> NutritionPlan:
    need = resolve_need(strain)
    req = yan_required(initial_brix, need)
    additional = max(0.0, req - juice_yan)
    warnings: list[str] = []
    if additional > 150:
        warnings.append(
            f"Additional YAN {additional:.0f} ppm exceeds the planner's 150 ppm "
            f"ceiling; capping at the 101-150 band."
        )
    band = additional_yan_band(additional)

    trigger_2_3 = round(initial_brix - 2.5, 1)      # 2-3 Brix drop (midpoint)
    trigger_third = round(initial_brix * (2.0 / 3.0), 1)  # 1/3 of sugar consumed
    stage_trigger = {_STAGE_2_3_DROP: trigger_2_3, _STAGE_THIRD: trigger_third}
    stage_label = {_STAGE_2_3_DROP: "2-3 °Brix drop",
                   _STAGE_THIRD: "1/3 sugar depletion"}

    adds = [NutrientAdd(
        stage="At rehydration",
        product="Go-Ferm Sterol Flash",
        dose_g_hl=GO_FERM_G_HL,
        grams=round(g_per_hl_to_grams(GO_FERM_G_HL, volume_gal), 1),
        lb_per_1000gal=round(g_per_hl_to_lb_per_1000gal(GO_FERM_G_HL), 2),
        trigger_brix=None,
        note="With yeast at inoculation.",
    )]

    for stage_key, product, dose, substituted in _SECURITY_PLAN[band]:
        adds.append(NutrientAdd(
            stage=stage_label[stage_key],
            product=product,
            dose_g_hl=dose,
            grams=round(g_per_hl_to_grams(dose, volume_gal), 1),
            lb_per_1000gal=round(g_per_hl_to_lb_per_1000gal(dose), 2),
            trigger_brix=stage_trigger[stage_key],
            note="Fermaid O substituted for Fermaid K." if substituted else "",
        ))

    return NutritionPlan(
        initial_brix=initial_brix, juice_yan=juice_yan, strain=strain, need=need,
        volume_gal=volume_gal, yan_required=round(req, 1),
        additional_yan=round(additional, 1), band=band, adds=adds,
        warnings=warnings,
    )
