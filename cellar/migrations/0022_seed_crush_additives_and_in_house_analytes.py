"""
Two independent seeds bundled together, both idempotent/adoptive:

  1. Re-run the analyte installer now that ANALYTES carries an `in_house`
     column — this adds the "temperature" analyte and marks brix/temperature
     in_house=True, which the in-house lab-entry form (Movement > Labs) reads
     to hide everything except those two.
  2. Mark the four named crush additives (KMBS, Tannins, Booster Rouge, Color
     Pro) crush_addition=True so they show in the crush/intake picker. Matches
     an existing row by name (case-insensitive, common alias) first — never
     creates a duplicate of something Nate already entered under a slightly
     different name — and only creates a new placeholder row if genuinely
     missing (category defaults to "other"; edit it from the Additives page
     if that's not right).
"""
from django.db import migrations

from cellar import reference_data

CRUSH_ADDITIVE_NAMES = [
    ("KMBS", ["kmbs", "potassium metabisulfite"]),
    ("Tannins", ["tannins", "tannin"]),
    ("Booster Rouge", ["booster rouge"]),
    ("Color Pro", ["color pro", "colorpro"]),
]


def forwards(apps, schema_editor):
    LabAnalyte = apps.get_model("cellar", "LabAnalyte")
    LabAnalyteSynonym = apps.get_model("cellar", "LabAnalyteSynonym")
    reference_data.install_analytes(LabAnalyte, LabAnalyteSynonym)

    Additive = apps.get_model("cellar", "Additive")
    for canonical, aliases in CRUSH_ADDITIVE_NAMES:
        match = None
        for a in Additive.objects.all():
            n = (a.name or "").strip().lower()
            if n == canonical.lower() or n in aliases:
                match = a
                break
        if match is None:
            match = Additive.objects.create(
                name=canonical, category="other", unit="g", dose_mode="per_volume")
        if not match.crush_addition:
            match.crush_addition = True
            match.save(update_fields=["crush_addition"])


def backwards(apps, schema_editor):
    """No-op — additive/analyte flags are safe to leave set; nothing to
    reverse without risking a row other data now points at."""


class Migration(migrations.Migration):

    dependencies = [
        ("cellar", "0021_externaldestination_additive_crush_addition_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
