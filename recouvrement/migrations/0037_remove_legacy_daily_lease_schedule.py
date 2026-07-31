from django.db import migrations


LEGACY_SCHEDULE_NAME = 'Génération Quotidienne des Échéances (Leases)'
LEGACY_TASK_PATH = 'core.tasks.generer_leases_quotidien_task'


def supprimer_schedule_quotidien(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(
        name=LEGACY_SCHEDULE_NAME,
    ).delete()
    Schedule.objects.filter(
        func=LEGACY_TASK_PATH,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('django_q', '0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more'),
        ('recouvrement', '0036_alter_contrat_prochaine_echeance'),
    ]

    operations = [
        migrations.RunPython(
            supprimer_schedule_quotidien,
            migrations.RunPython.noop,
        ),
    ]
