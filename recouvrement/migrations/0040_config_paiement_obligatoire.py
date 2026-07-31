import django.db.models.deletion
from django.db import migrations, models


def affecter_configurations_existantes(apps, schema_editor):
    ConfigPaiement = apps.get_model('accounts', 'ConfigPaiement')
    Contrat = apps.get_model('recouvrement', 'Contrat')
    SessionPaiement = apps.get_model('recouvrement', 'SessionPaiement')

    compte_ids = set(
        Contrat.objects
        .filter(config_paiement__isnull=True)
        .values_list('compte_id', flat=True)
    )
    compte_ids.update(
        SessionPaiement.objects
        .filter(config_paiement__isnull=True)
        .values_list('compte_id', flat=True)
    )

    comptes_sans_configuration = []
    for compte_id in sorted(compte_ids):
        configurations = ConfigPaiement.objects.filter(compte_id=compte_id)
        config = (
            configurations.filter(par_defaut=True).first()
            or configurations.order_by('-actif', 'id').first()
        )
        if config is None:
            comptes_sans_configuration.append(compte_id)
            continue

        Contrat.objects.filter(
            compte_id=compte_id,
            config_paiement__isnull=True,
        ).update(config_paiement_id=config.id)
        SessionPaiement.objects.filter(
            compte_id=compte_id,
            config_paiement__isnull=True,
        ).update(config_paiement_id=config.id)

    if comptes_sans_configuration:
        comptes = ', '.join(map(str, comptes_sans_configuration))
        raise RuntimeError(
            "Impossible de rendre la configuration de paiement obligatoire : "
            f"aucune configuration n'existe pour le(s) compte(s) {comptes}."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_configpaiement_multi_configuration'),
        ('recouvrement', '0039_contrat_session_config_paiement'),
    ]

    operations = [
        migrations.RunPython(
            affecter_configurations_existantes,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='contrat',
            name='config_paiement',
            field=models.ForeignKey(
                help_text=(
                    "Configuration obligatoire utilisée pour encaisser les "
                    "paiements de ce contrat."
                ),
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
                help_text=(
                    "Configuration ayant servi à créer la transaction auprès "
                    "de la passerelle."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name='sessions_paiement',
                to='accounts.configpaiement',
                verbose_name='Configuration de paiement utilisée',
            ),
        ),
    ]
