import django.db.models.deletion
from django.db import migrations, models


def memoriser_config_sur_sessions_existantes(apps, schema_editor):
    ConfigPaiement = apps.get_model('accounts', 'ConfigPaiement')
    SessionPaiement = apps.get_model('recouvrement', 'SessionPaiement')

    configurations = dict(
        ConfigPaiement.objects
        .filter(par_defaut=True)
        .values_list('compte_id', 'id')
    )
    for compte_id, config_id in configurations.items():
        SessionPaiement.objects.filter(
            compte_id=compte_id,
            config_paiement__isnull=True,
        ).update(config_paiement_id=config_id)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_configpaiement_multi_configuration'),
        ('recouvrement', '0038_regle_generation_nom_par_compte'),
    ]

    operations = [
        migrations.AddField(
            model_name='contrat',
            name='config_paiement',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Laisser vide pour utiliser la configuration par "
                    "défaut du compte."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='contrats',
                to='accounts.configpaiement',
                verbose_name='Configuration de paiement particulière',
            ),
        ),
        migrations.AddField(
            model_name='sessionpaiement',
            name='config_paiement',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Configuration ayant servi à créer la transaction "
                    "auprès de la passerelle."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='sessions_paiement',
                to='accounts.configpaiement',
                verbose_name='Configuration de paiement utilisée',
            ),
        ),
        migrations.RunPython(
            memoriser_config_sur_sessions_existantes,
            migrations.RunPython.noop,
        ),
    ]
