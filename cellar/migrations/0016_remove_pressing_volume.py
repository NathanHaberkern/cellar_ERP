from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cellar', '0015_seed_removal_basis'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='pressingevent',
            name='volume',
        ),
    ]
