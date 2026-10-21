"""Seed box sizes and bottle weights from Nate's figures (Oct 21, 2026).

Adoptive and idempotent, same shape as cellar 0009/0013: a row that already
exists (matched on name / ml) is left exactly as it is, so re-running this
never overwrites a value Nate has since corrected in the admin. Reverse is a
no-op because packages will point at these boxes.
"""
from decimal import Decimal as D

from django.db import migrations

# name, use, L, W, H (in), tare lb, max bottles, max weight lb
BOXES = [
    ("1 Bottle", "standard", "15.5", "5", "5.5", "0.6", 1, "4"),
    ("2 Bottle", "standard", "15", "10", "5.5", "1.1", 2, "8"),
    ("3 Bottle", "standard", "20", "12.75", "6.5", "1.9", 3, "12"),
    ("4 Bottle", "standard", "20", "15", "5.5", "1.9", 4, "17"),
    ("6 Bottle", "standard", "14", "9.75", "14", "2.3", 6, "22"),
    ("12 Bottle", "standard", "18.5", "14", "14.5", "4.9", 12, "42"),
    ("Port", "port_club", "16", "13", "4.5", "2", 1, "8"),
    ("Magnum", "magnum", "6", "6", "18", "0.7", 1, "10"),
]

# ml, label, packed weight per bottle (lb)
BOTTLES = [
    (500, "500 ml port", "2.16"),
    (750, "750 ml wine", "3.0"),
    (1500, "Magnum (1.5 L)", "7.5"),
]


def forwards(apps, schema_editor):
    BoxSize = apps.get_model("shipping", "BoxSize")
    BottleWeight = apps.get_model("shipping", "BottleWeight")
    for order, (name, use, l, w, h, tare, n, maxw) in enumerate(BOXES):
        BoxSize.objects.get_or_create(name=name, defaults=dict(
            use=use, length_in=D(l), width_in=D(w), height_in=D(h), tare_lb=D(tare),
            max_bottles=n, max_weight_lb=D(maxw), sort_order=order * 10))
    for ml, label, lb in BOTTLES:
        BottleWeight.objects.get_or_create(ml=ml, defaults=dict(label=label, weight_lb=D(lb)))


def backwards(apps, schema_editor):
    """No-op: packages reference these rows."""


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
