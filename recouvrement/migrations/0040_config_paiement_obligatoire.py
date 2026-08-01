from django.db import migrations


def affecter_configurations_existantes_si_disponibles(apps, schema_editor):
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

    for compte_id in sorted(compte_ids):
        configurations = ConfigPaiement.objects.filter(compte_id=compte_id)
        config = (
            configurations.filter(par_defaut=True).first()
            or configurations.order_by('-actif', 'id').first()
        )
        if config is None:
            # La configuration Mobile Money est facultative. Les contrats
            # et sessions de ce compte restent donc volontairement à NULL.
            continue

        Contrat.objects.filter(
            compte_id=compte_id,
            config_paiement__isnull=True,
        ).update(config_paiement_id=config.id)
        SessionPaiement.objects.filter(
            compte_id=compte_id,
            config_paiement__isnull=True,
        ).update(config_paiement_id=config.id)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_configpaiement_multi_configuration'),
        ('recouvrement', '0039_contrat_session_config_paiement'),
    ]

    operations = [
        migrations.RunPython(
            affecter_configurations_existantes_si_disponibles,
            migrations.RunPython.noop,
        ),
    ]
