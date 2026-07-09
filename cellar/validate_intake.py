"""Reconstruct the 2025 Mohr-Fry Zinfandel Lot #2 intake against the real
models + seed, and assert the computed outputs match Nate's handwritten notes.

    python validate_intake.py
"""
import os, django, uuid
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.utils import timezone
from cellar.models import (
    Variety, Grower, Vineyard, Block, VarietalDesignation, Program,
    HarvestEvent, WeighTag, SourceType, Lot, TankAssignment, DestemmingEvent,
    Addition,
)
from cellar.services import operations as ops


def ok(cond, label, got=""):
    print(f"  {'OK ' if cond else 'FAIL'}  {label}{('  → ' + str(got)) if got else ''}")
    assert cond, label


print("=" * 70)
print("Reconstruct: 2025 Mohr-Fry Zinfandel Lot #2 — destem 7.69 T to SS-8 (Path D)")
print("=" * 70)

# --- minimal reference master (idempotent) ---
zin, _ = Variety.objects.get_or_create(name="Zinfandel")
grower, _ = Grower.objects.get_or_create(name="Mohr-Fry Ranches",
                                         defaults={"source_type": SourceType.PURCHASED})
vyd, _ = Vineyard.objects.get_or_create(grower=grower, name="Mohr-Fry",
                                        defaults={"crush_district": 11})
block, _ = Block.objects.get_or_create(vineyard=vyd, name="416", defaults={"variety": zin})
VarietalDesignation.objects.get_or_create(
    variety=zin, program=Program.TABLE, block=None, vineyard=None,
    defaults={"abbreviation": "MZ", "is_curated": True})

he = HarvestEvent.objects.create(block=block, harvest_date=datetime(2025, 8, 25).date())
wt = WeighTag.objects.create(
    weigh_tag_number=f"MZ2-{uuid.uuid4().hex[:6]}", harvest_event=he,
    source_type=SourceType.PURCHASED, disposition=WeighTag.Disposition.CRUSHED,
    net_weight_lbs=Decimal("15380"))   # 7.69 tons

destem_at = timezone.make_aware(datetime(2025, 8, 25, 9, 0))   # a Monday

# --- receive + destem (Path D, into tank SS-8) ---
r = ops.receive_and_destem(
    vintage=25, variety=zin, program=Program.TABLE, path=DestemmingEvent.Path.D,
    destem_at=destem_at, allocations=[(wt, Decimal("15380"))],
    tank_code="SS-8", initial_temp_f=Decimal("60"))

lot = r["lot"]
print(f"\n  Lot created: {r['code']}  (status {lot.status})")
ok("MZ" in r["code"], "lot code carries the MZ abbreviation", r["code"])
ok(r["tons"] == Decimal("7.69"), "tons = 7.69", r["tons"])
ok(r["intake_volume_est_gal"] == Decimal("1307"),
   "intake est = 7.69 × 170 = 1307 gal (note: ~1307)", r["intake_volume_est_gal"])
ok(r["press_yield_est_gal"] == Decimal("1269"),
   "press-yield est = 7.69 × 165 fallback = 1269 gal", r["press_yield_est_gal"])
ok(lot.status == Lot.Status.COLD_SOAK, "status advanced to Cold soak (Path D)", lot.status)
ok(str(r["target_inoc_date"]) == "2025-08-28",
   "inoculation auto-scheduled Mon+3 working days → Thu 8/28", r["target_inoc_date"])
ok(TankAssignment.objects.filter(lot=lot, vessel__code="SS-8").exists(),
   "assigned to SS-8")
ok(ops.current_volume(lot) == Decimal("1307.0"), "running volume persisted = 1307",
   ops.current_volume(lot))

print("\n  --- crusher additions (computed from default rates) ---")
so2 = ops.record_addition(lot, "KMBS", added_at=destem_at)            # ppm_target, default 40
ok(342 <= float(so2.quantity) <= 345,
   f"40 ppm SO₂ on 1307 gal → {so2.computed_dose}", so2.quantity)

ta = ops.record_addition(lot, "Tartaric", added_at=destem_at)        # per_volume, default 3.5 lb/1000gal
ok(abs(float(ta.quantity) - 4.57) < 0.02,
   f"Tartaric 3.5 lb/1000gal on 1307 gal → {ta.quantity:.2f} lb (note: 4.6 lb)", ta.quantity)

print("\n  --- inoculation + nutrition plan (D21, 25.1 Brix, juice YAN 220) ---")
ev, plan = ops.inoculate(lot, inoculated_at=destem_at, yeast_strain="D21",
                         initial_brix=Decimal("25.1"), juice_yan=Decimal("220"))
gf = Addition.objects.filter(lot=lot, additive__name="Go-Ferm Sterol Flash").first()
ok(gf is not None and abs(float(gf.quantity) - 1484) < 3,
   f"Go-Ferm 30 g/hL on 1307 gal → {gf.quantity:.0f} g", gf.quantity if gf else None)
print(f"    need={plan.need}  YAN req={plan.yan_required}  additional={plan.additional_yan}  band={plan.band}")
for a in plan.adds:
    trig = "at inoculation" if a.trigger_brix is None else f"~{a.trigger_brix} Brix"
    print(f"      · {a.stage:20s} {a.product:22s} {a.dose_g_hl:>2} g/hL  @ {trig}")

# a real Fermaid O add logged at the note's rate reproduces the recorded grams
fo = ops.record_addition(lot, "Fermaid O", added_at=destem_at, rate_override=Decimal("10"))
ok(abs(float(fo.quantity) - 494.8) < 0.6,
   f"Fermaid O 10 g/hL on 1307 gal → {fo.quantity:.1f} g (note recorded 494.8 g)", fo.quantity)

print("\n  --- addition ledger for this lot ---")
for a in Addition.objects.filter(lot=lot).order_by("id"):
    print(f"      {a.additive.name:22s} target={a.target:14s} dose={a.computed_dose}")

print("\nAll intake assertions passed.")
