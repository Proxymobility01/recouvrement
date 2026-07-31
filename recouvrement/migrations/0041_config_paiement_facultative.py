import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0010_remove_configpaiement_par_defaut'),
        ('recouvrement', '0040_config_paiement_obligatoire'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contrat',
            name='config_paiement',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Configuration utilisée pour les paiements Mobile Money. "
                    "Elle peut rester vide pour les autres moyens de paiement."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='contrats',
                to='accounts.configpaiement',
                verbose_name='Configuration de paiement',
            ),
        ),
        migrations.AlterField(
            model_name='sessionpaiement',
            name='config_paiement',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Configuration ayant servi à créer une transaction Mobile "
                    "Money. Elle peut rester vide pour les autres paiements."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='sessions_paiement',
                to='accounts.configpaiement',
                verbose_name='Configuration de paiement utilisée',
            ),
        ),
    ]
