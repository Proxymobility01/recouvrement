from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recouvrement', '0037_remove_legacy_daily_lease_schedule'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reglegenerationlease',
            name='nom',
            field=models.CharField(
                help_text=(
                    "Ex: 'Classique 24h', 'Demi-journée 12h/22h', "
                    "'Hebdomadaire'..."
                ),
                max_length=100,
            ),
        ),
        migrations.AddConstraint(
            model_name='reglegenerationlease',
            constraint=models.UniqueConstraint(
                fields=('compte_id', 'nom'),
                name='unique_regle_generation_nom_par_compte',
            ),
        ),
    ]
