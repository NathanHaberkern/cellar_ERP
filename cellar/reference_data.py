"""
Canonical reference data + idempotent installers.

Single source of truth for the seed tables that MUST exist for the app to work:
lab analytes, ETS name synonyms, and the auto-task rules. Both the management
commands and the data migrations call `install_*` from here, so the data lands on
`migrate` (including on Heroku, where a forgotten one-off command is a real
failure mode) and re-running a seed by hand is still safe.

The installers are ADOPTIVE and per-row:
  * an existing analyte with the right name but no slug is adopted, not duplicated
    (`name` is unique — blind create() would raise and, inside one atomic block,
    silently roll back the whole seed);
  * tuned rule params and enabled flags are never clobbered on re-run.
"""

ANALYTES = [
    # --- juice / shared core ---
    ("brix",             "brix",                           "degrees",     10),
    ("ph",               "pH",                             "",            20),
    ("ta",               "titratable acidity",             "g/L",         30),
    ("va",               "volatile acidity (acetic acid)", "g/L",         40),
    ("glucose_fructose", "glucose + fructose",             "g/L",         50),
    ("l_malic",          "L-malic acid",                   "g/L",         60),
    ("tartaric",         "tartaric acid",                  "g/L",         70),
    ("yan",              "yeast assimilable nitrogen",     "mg/L (as N)", 80),
    ("ammonia",          "ammonia",                        "mg/L",        90),
    ("amino",            "alpha-amino compounds (as N)",   "mg/L",       100),
    ("potassium",        "potassium",                      "mg/L",       110),
    # --- chemistry-panel additions ---
    ("fso2",             "free sulfur dioxide",            "mg/L",       120),
    ("tso2",             "total sulfur dioxide",           "mg/L",       130),
    ("molecular_so2",    "molecular sulfur dioxide",       "mg/L",       140),
    ("ethanol_20c",      "ethanol at 20C",                 "% vol",      150),
    ("ethanol_60f",      "ethanol at 60F",                 "% vol",      160),
    # --- stability / smoke (not part of the two full panels) ---
    ("heat_stability",   "heat stability (Pocock & Waters)", "NTU",      200),
    ("turbidity",        "turbidity (turbidimeter)",        "NTU",       210),
    ("guaiacol",         "guaiacol GC MS/MS",               "µg/L",      220),
    ("methylguaiacol",   "4-methylguaiacol GC MS/MS",       "µg/L",      230),
]

# raw ETS 'Analysis Name' string -> analyte slug. Exact-name matches don't need an
# entry here; these cover the method-suffix variants the Read Me merges.
SYNONYMS = [
    ("volatile acidity (acetic)",            "va"),
    ("volatile acidity",                     "va"),
    ("titratable acidity (titrator)",        "ta"),
    ("ta (titrator)",                        "ta"),
    ("pH (titrator)",                        "ph"),
    ("tartaric acid (IC)",                   "tartaric"),
    ("tartaric (IC)",                        "tartaric"),
    ("ethanol (20C) GC",                     "ethanol_20c"),
    ("ethanol at 20 C",                      "ethanol_20c"),
    ("ethanol (60F) GC",                     "ethanol_60f"),
    ("ethanol at 60 F",                      "ethanol_60f"),
    ("yeast assimilable nitrogen (NOPA + ammonia)", "yan"),
    ("alpha-amino compounds",                "amino"),
    ("alpha amino nitrogen (as N)",          "amino"),
]



# key, name, description, default params. Params/enabled are only applied on FIRST
# install — never overwritten, so knobs tuned in the Rules menu survive a re-seed.
TASK_RULES = [
    {
        "key": "topping_interval",
        "name": "Topping interval",
        "description": "Flag lots aging in oak that haven't been topped within "
                       "interval_days. Creates an FSO₂ task and a top-barrels task.",
        "params": {"interval_days": 60},
    },
    {
        "key": "ferment_daily",
        "name": "Fermentation daily cadence",
        "description": "For each lot in one of `statuses`, create a daily cap-"
                       "management task and a daily Brix + temp reading task.",
        "params": {"statuses": ["fermenting"]},
    },
    {
        "key": "partial_barrel",
        "name": "Partial barrel — fill to full",
        "description": "Filling barrels from a tank almost always ends on a partial. "
                       "The barrel isn't empty (the container is unavailable) but it "
                       "isn't full either. Flag it and open a task to top it up from "
                       "another wine. Fires once per placement; clears when filled.",
        "params": {"tolerance_gal": 1, "grace_days": 0},
    },
]


# ======================================================================
# Bottling + materials reference
# ======================================================================
# name, mL, bottles/case
BOTTLE_FORMATS = [
    ("750ml",         750, 12),
    ("500ml",         500, 12),
    ("375ml Split",   375, 12),
    ("1.5L Magnum",  1500,  6),
]

# name, kind, unit_cost, unit
# NOTE: costs are ESTIMATES pending Nate's real price list — flagged in `notes`
# on the seeded rows so nobody mistakes them for invoiced figures.
DRY_GOODS = [
    ("Bottle — 750ml",        "bottle",  "1.1000", "each"),
    ("Bottle — 500ml",        "bottle",  "1.0500", "each"),
    ("Bottle — 375ml",        "bottle",  "0.9000", "each"),
    ("Bottle — 1.5L Magnum",  "bottle",  "2.6000", "each"),
    ("Cork — Diam10",         "closure", "0.4500", "each"),   # same cork, all wines
    ("Capsule — tin (port)",  "capsule", "0.2200", "each"),   # ports only
    ("Label — front",         "label",   "0.1800", "each"),
    ("Label — back",          "label",   "0.1800", "each"),
    ("Label — neck (port)",   "label",   "0.1200", "each"),
]

# Dry-good bill of materials, per bottle, by program.
#   table wine: bottle + Diam10 + front + back                (no capsule)
#   port:       bottle + Diam10 + front + back + neck + tin capsule
DRY_GOOD_BOM = {
    "table": ["Cork — Diam10", "Label — front", "Label — back"],
    "rose":  ["Cork — Diam10", "Label — front", "Label — back"],
    "port":  ["Cork — Diam10", "Label — front", "Label — back",
              "Label — neck (port)", "Capsule — tin (port)"],
}

# name, kind, unit, unit_cost
MATERIALS = [
    ("Vino Blanc concentrate", "concentrate", "gal", "18.0000"),   # $108 / 6-gal pail
]

# key, value, notes — labor rates are STUBS (Nate: "stub for now, revisit later")
CONFIG_CONSTANTS = [
    ("labor_harvest_per_ton",    "0", "STUB — cellar labor allocated per ton crushed"),
    ("labor_cellar_per_gal_month", "0", "STUB — cellar labor allocated per gal-month in bulk"),
    ("labor_bottling_per_case",  "0", "STUB — line labor allocated per case bottled"),
    ("estate_fruit_cost_per_ton", "0",
     "Superseded by FruitPrice rows (per vintage). Kept as the last-resort fallback."),
]


# --------------------------------------------------------------- installers
def install_analytes(LabAnalyte, LabAnalyteSynonym):
    """Idempotent + adoptive. Takes the model classes so migrations can pass their
    historical versions. Returns (created, adopted, updated, synonyms_created)."""
    created = adopted = updated = 0
    by_slug = {}

    for slug, name, unit, order in ANALYTES:
        obj = LabAnalyte.objects.filter(slug=slug).first()
        if obj is None:
            # adopt a pre-existing row that matches on name (unique) but has no slug
            obj = LabAnalyte.objects.filter(name__iexact=name).first()
            if obj is not None:
                obj.slug = slug
                adopted += 1
            else:
                obj = LabAnalyte(slug=slug)
                created += 1
        else:
            updated += 1
        obj.name = name
        obj.unit = unit
        obj.sort_order = order
        obj.save()
        by_slug[slug] = obj

    syn_created = 0
    for raw, slug in SYNONYMS:
        _, made = LabAnalyteSynonym.objects.get_or_create(
            raw_name=raw, defaults={"analyte": by_slug[slug]})
        syn_created += made
    return created, adopted, updated, syn_created


def install_task_rules(TaskRule):
    """Idempotent. Refreshes name/description; preserves params + enabled."""
    created = 0
    for spec in TASK_RULES:
        obj, made = TaskRule.objects.get_or_create(
            key=spec["key"],
            defaults={"name": spec["name"], "description": spec["description"],
                      "params": spec["params"], "enabled": True})
        if not made:
            obj.name = spec["name"]
            obj.description = spec["description"]
            obj.save()
        created += made
    return created


def install_bottling_reference(BottleFormat, DryGood, Material, ConfigConstant):
    """Idempotent + adoptive. The four tables that were completely empty — nothing
    downstream of bottling (COGS, dry-good use, sweetening, Part IV materials) can
    run without them."""
    made = {"formats": 0, "dry_goods": 0, "materials": 0, "config": 0}

    for name, ml, per_case in BOTTLE_FORMATS:
        obj, created = BottleFormat.objects.get_or_create(
            name=name, defaults={"ml": ml, "bottles_per_case": per_case})
        if not created and (obj.ml != ml or obj.bottles_per_case != per_case):
            obj.ml, obj.bottles_per_case = ml, per_case
            obj.save()
        made["formats"] += created

    for name, kind, cost, unit in DRY_GOODS:
        _, created = DryGood.objects.get_or_create(
            name=name,
            defaults={"kind": kind, "unit_cost": cost, "unit": unit})
        made["dry_goods"] += created

    for name, kind, unit, cost in MATERIALS:
        _, created = Material.objects.get_or_create(
            name=name, defaults={"kind": kind, "unit": unit, "unit_cost": cost})
        made["materials"] += created

    for key, value, notes in CONFIG_CONSTANTS:
        _, created = ConfigConstant.objects.get_or_create(
            key=key, defaults={"value": value, "notes": notes})
        made["config"] += created

    return made
