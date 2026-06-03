# statistiques/apps.py

import logging
from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


def setup_scheduled_tasks(sender, **kwargs):
    """
    S'exécute automatiquement après les migrations.
    Garantit que les tables de Django Q2 existent avant d'insérer le schedule.
    """
    from django_q.models import Schedule

    try:
        Schedule.objects.update_or_create(
            name='Rafraîchissement des Statistiques',
            defaults={
                'func': 'core.tasks.rafraichir_statistiques_horaire_task',
                'schedule_type': Schedule.HOURLY,
                'repeats': -1,
            }
        )
        logger.info("✅ Tâche planifiée 'Rafraîchissement des Statistiques' configurée avec succès (Exécution horaire).")

    except Exception as e:
        logger.warning(f"❌ Impossible de configurer la tâche planifiée des statistiques : {e}")


class StatistiquesConfig(AppConfig):
    name = 'statistiques'

    def ready(self):
        post_migrate.connect(setup_scheduled_tasks, sender=self)