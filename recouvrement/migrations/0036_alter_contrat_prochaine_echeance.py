# Generated manually to support rules with no next occurrence (Schedule.ONCE).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recouvrement', '0035_alter_lease_date_echeance'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contrat',
            name='prochaine_echeance',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
