"""
Seed the canonical lab-analyte master list + ETS name synonyms.

The analyte set and units are taken verbatim from St. Amant's cleaned 5-year ETS
export (the 'Analysis' column, which already merges ETS's method-suffixed names).
`slug` is the stable key the importer and the panel definitions reference; the
display `name` matches how ETS prints it so an exact-name match is the common path,
with synonyms covering the method-suffix variants.

Idempotent: safe to re-run. Updates unit / name / sort order in place, never
deletes. Run after `migrate`:  python manage.py seed_lab_analytes
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from cellar.models import LabAnalyte, LabAnalyteSynonym

# slug, display name (as ETS prints it), unit, sort order
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


class Command(BaseCommand):
    help = "Seed canonical lab analytes and ETS analysis-name synonyms."

    @transaction.atomic
    def handle(self, *args, **opts):
        made = updated = 0
        by_slug = {}
        for slug, name, unit, order in ANALYTES:
            obj, created = LabAnalyte.objects.update_or_create(
                slug=slug,
                defaults={"name": name, "unit": unit, "sort_order": order},
            )
            by_slug[slug] = obj
            made += created
            updated += (0 if created else 1)

        syn_made = 0
        for raw, slug in SYNONYMS:
            _, created = LabAnalyteSynonym.objects.update_or_create(
                raw_name=raw, defaults={"analyte": by_slug[slug]})
            syn_made += created

        self.stdout.write(self.style.SUCCESS(
            f"Analytes: {made} created, {updated} updated. "
            f"Synonyms: {syn_made} created / {len(SYNONYMS)} total."))
