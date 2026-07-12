"""
Install the reference data the app cannot run without: lab analytes, ETS name
synonyms, and the auto-task rules.

These were previously one-off `seed_*` commands, which is exactly the kind of step
that gets skipped — and when it is, the symptom is baffling rather than loud (every
ETS analysis reports "unmapped", the Rules page sits empty). Making it a migration
means the data lands on `migrate`, locally and on every Heroku release.

Idempotent and adoptive (see cellar/reference_data.py): re-running adopts existing
rows instead of duplicating them, and never overwrites rule params you've tuned.
Reverse is a no-op — we don't delete reference data other rows point at.
"""
from django.db import migrations

from cellar import reference_data


def forwards(apps, schema_editor):
    LabAnalyte = apps.get_model("cellar", "LabAnalyte")
    LabAnalyteSynonym = apps.get_model("cellar", "LabAnalyteSynonym")
    TaskRule = apps.get_model("cellar", "TaskRule")

    reference_data.install_analytes(LabAnalyte, LabAnalyteSynonym)
    reference_data.install_task_rules(TaskRule)


def backwards(apps, schema_editor):
    """No-op. Lab results and tasks reference these rows; dropping them on an
    unapply would cascade into real data."""


class Migration(migrations.Migration):

    dependencies = [
        ("cellar", "0008_taskrule_task_taskevent"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
