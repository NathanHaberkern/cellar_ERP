"""
Install the four reference tables that were completely empty: BottleFormat,
DryGood, Material, ConfigConstant — plus the new partial-barrel task rule.

Nothing downstream of bottling could run without these. `bottle_parcel()` needs a
BottleFormat, `bottling_cogs()` needs DryGood costs, `SweeteningEvent` needs a
Material to book the Part IV concentrate use against, and the costing service has
been reading an `estate_fruit_cost_per_ton` ConfigConstant that has never existed.

Dry-good and bottle costs here are ESTIMATES — Nate doesn't have the price list to
hand. They're labelled as such on the rows. Replace them before anyone quotes a
COGS number to an accountant.

Same shape as 0009: adoptive, per-row, idempotent, no-op reverse.
"""
from django.db import migrations

from cellar import reference_data


def forwards(apps, schema_editor):
    BottleFormat = apps.get_model("cellar", "BottleFormat")
    DryGood = apps.get_model("cellar", "DryGood")
    Material = apps.get_model("cellar", "Material")
    ConfigConstant = apps.get_model("cellar", "ConfigConstant")
    TaskRule = apps.get_model("cellar", "TaskRule")

    reference_data.install_bottling_reference(
        BottleFormat, DryGood, Material, ConfigConstant)
    reference_data.install_task_rules(TaskRule)


def backwards(apps, schema_editor):
    """No-op. Bottling runs and sweetenings point at these rows."""


class Migration(migrations.Migration):

    dependencies = [
        ("cellar", "0012_fruitprice"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
