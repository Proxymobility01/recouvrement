from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0009_configpaiement_multi_configuration'),
        ('recouvrement', '0040_config_paiement_obligatoire'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='configpaiement',
            name='unique_config_paiement_defaut_par_compte',
        ),
        migrations.RemoveField(
            model_name='configpaiement',
            name='par_defaut',
        ),
    ]
