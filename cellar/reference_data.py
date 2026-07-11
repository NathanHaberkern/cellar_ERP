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
