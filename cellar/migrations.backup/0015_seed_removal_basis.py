"""
Install the `excise_removal_basis` constant.

0013 already ran on any database that has it, and the installer is get_or_create,
so a new CONFIG_CONSTANTS entry added after the fact never lands. Re-run the
installer in its own migration; it is idempotent and adoptive, so this is a no-op
for everything 0013 already put in place.
"""
from django.db import migrations

from cellar import reference_data


def forwards(apps, schema_editor):
    reference_data.install_bottling_reference(
        apps.get_model("cellar", "BottleFormat"),
        apps.get_model("cellar", "DryGood"),
        apps.get_model("cellar", "Material"),
        apps.get_model("cellar", "ConfigConstant"),
    )


def backwards(apps, schema_editor):
    """No-op."""


class Migration(migrations.Migration):
    dependencies = [("cellar", "0014_fortification_kind")]
    operations = [migrations.RunPython(forwards, backwards)]
