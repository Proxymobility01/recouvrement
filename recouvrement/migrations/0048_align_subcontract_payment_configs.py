from django.db import migrations
from django.utils import timezone


def aligner_configurations_sous_contrats(apps, schema_editor):
    Contrat = apps.get_model('recouvrement', 'Contrat')
    maintenant = timezone.now()

    parents = (
        Contrat.objects
        .filter(parent__isnull=True)
        .values_list('pk', 'config_paiement_id')
        .iterator()
    )
    for parent_id, config_paiement_id in parents:
        sous_contrats = Contrat.objects.filter(parent_id=parent_id)
        if config_paiement_id is None:
            sous_contrats = sous_contrats.filter(
                config_paiement__isnull=False,
            )
        else:
            sous_contrats = sous_contrats.exclude(
                config_paiement_id=config_paiement_id,
            )

        sous_contrats.update(
            config_paiement_id=config_paiement_id,
            updated_at=maintenant,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_unique_config_paiement_defaut_par_compte'),
        ('recouvrement', '0047_unique_regle_generation_defaut_par_compte'),
    ]

    operations = [
        migrations.RunPython(
            aligner_configurations_sous_contrats,
            migrations.RunPython.noop,
        ),
    ]
