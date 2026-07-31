from django.db import migrations, models
from django.db.models import Q


def definir_configurations_existantes_par_defaut(apps, schema_editor):
    ConfigPaiement = apps.get_model('accounts', 'ConfigPaiement')
    ConfigPaiement.objects.update(par_defaut=True)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_remove_configpaiement_webhook_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='configpaiement',
            name='nom',
            field=models.CharField(
                default='Configuration principale',
                help_text=(
                    "Nom interne permettant d'identifier la destination "
                    "des paiements, par exemple : Compte principal ou "
                    "Flotte premium."
                ),
                max_length=100,
                verbose_name='Nom de la configuration',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='configpaiement',
            name='actif',
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Une configuration inactive ne peut plus être utilisée "
                    "pour initier de nouveaux paiements."
                ),
            ),
        ),
        migrations.AddField(
            model_name='configpaiement',
            name='par_defaut',
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Utilisée par les contrats du compte qui n'ont aucune "
                    "configuration particulière."
                ),
                verbose_name='Configuration par défaut',
            ),
        ),
        migrations.RunPython(
            definir_configurations_existantes_par_defaut,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name='configpaiement',
            name='unique_config_paiement_par_compte',
        ),
        migrations.AddConstraint(
            model_name='configpaiement',
            constraint=models.UniqueConstraint(
                fields=('compte_id', 'nom'),
                name='unique_config_paiement_nom_par_compte',
            ),
        ),
        migrations.AddConstraint(
            model_name='configpaiement',
            constraint=models.UniqueConstraint(
                condition=Q(par_defaut=True),
                fields=('compte_id',),
                name='unique_config_paiement_defaut_par_compte',
            ),
        ),
        migrations.AlterModelOptions(
            name='configpaiement',
            options={
                'verbose_name': 'Configuration de paiement',
                'verbose_name_plural': 'Configurations de paiement',
            },
        ),
    ]
