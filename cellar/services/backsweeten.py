"""
Back-sweetening calculator — how much concentrate to reach a target residual sugar.

Sugar mass balance (Pearson's square):
    Vw·Sw + Vc·Sc = (Vw + Vc)·St
  → Vc = Vw · (St − Sw) / (Sc − St)

where V = volume (any consistent unit — gallons work), S = sugar concentration (g/L).
Work in residual sugar (g/L), which is the correct unit for back-sweetening a finished
(dry) wine; apparent Brix is confounded by alcohol once the wine has fermented.

The concentrate (unfermented) can be given in Brix; brix_to_sugar_gL converts it.
"""
from decimal import Decimal, ROUND_HALF_UP

TENTH = Decimal("0.1")


def brix_to_sugar_gL(brix):
    """Approx grams of sugar per litre for grape juice/concentrate at a given Brix.
    Uses an SG estimate; concentrate is unfermented so Brix is meaningful here."""
    b = float(brix)
    sg = 1 + b / (258.6 - (b * 227.1 / 258.2))     # Brix → specific gravity
    return Decimal(str(b * sg * 10)).quantize(TENTH)  # Brix (g/100g) × SG × 10 → g/L


def backsweeten(wine_gallons, wine_rs_gL, target_rs_gL, concentrate_sugar_gL):
    """Volume of concentrate to raise `wine_gallons` from wine_rs to target_rs.
    concentrate_sugar_gL: the concentrate's sugar in g/L (use brix_to_sugar_gL if you
    only have Brix). Returns concentrate gallons, final volume, and the achieved RS."""
    Vw = Decimal(str(wine_gallons))
    Sw = Decimal(str(wine_rs_gL))
    St = Decimal(str(target_rs_gL))
    Sc = Decimal(str(concentrate_sugar_gL))
    if St <= Sw:
        raise ValueError("target RS must exceed the wine's current RS")
    if Sc <= St:
        raise ValueError("concentrate sugar must exceed the target RS")
    Vc = Vw * (St - Sw) / (Sc - St)
    Vf = Vw + Vc
    achieved = (Vw * Sw + Vc * Sc) / Vf
    return {
        "concentrate_gallons": Vc.quantize(Decimal("0.01"), ROUND_HALF_UP),
        "final_gallons": Vf.quantize(TENTH, ROUND_HALF_UP),
        "achieved_rs_gL": achieved.quantize(TENTH, ROUND_HALF_UP),
        "sugar_added_gL": (St - Sw),
    }
