"""
Percent-of-volume dosing, and the Water additive that motivated it.

Water is dosed as a PERCENT of what's in the tank ("add 10% H2O"), and unlike
every other additive it physically GROSSES THE LOT UP by the gallons that go
in. Both halves of that were missing: there was no dose mode whose answer is a
volume, and `operations.add_water()` — which did know how to gross up — was
dead code nothing ever called.

Idempotent: update_or_create, so re-running against a DB that already has a
hand-entered "Water" row corrects it in place rather than colliding on the
unique name.
"""
from decimal import Decimal

from django.db import migrations, models


def seed_water(apps, schema_editor):
    Additive = apps.get_model("cellar", "Additive")
    Additive.objects.update_or_create(
        name="Water",
        defaults=dict(
            category="other",
            unit="gal",
            dose_mode="pct_volume",
            default_rate=Decimal("10"),
            rate_unit="% of volume",
            default_target_ppm=None,
            so2_fraction=None,
            crush_addition=False,
        ),
    )


def unseed_water(apps, schema_editor):
    apps.get_model("cellar", "Additive").objects.filter(name="Water").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cellar", "0023_mustsale"),
    ]

    operations = [
        migrations.AlterField(
            model_name="additive",
            name="dose_mode",
            field=models.CharField(
                choices=[
                    ("per_volume", "Rate per volume"),
                    ("per_ton", "Rate per ton of fruit"),
                    ("ppm_target", "SO₂ to target ppm"),
                    ("pct_volume", "Percent of volume (adds volume)"),
                    ("bench", "Bench trial (no default)"),
                ],
                default="per_volume",
                max_length=12,
            ),
        ),
        migrations.RunPython(seed_water, unseed_water),
    ]
