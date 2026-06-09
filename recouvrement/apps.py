import logging
from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


def setup_scheduled_tasks(sender, **kwargs):
    """
    S'exécute automatiquement après les migrations pour configurer les tâches asynchrones.
    """
    from django_q.models import Schedule

    try:
        # 1. Tâche des Leases : Tous les jours à 02h00 du matin
        Schedule.objects.update_or_create(
            name='Génération Quotidienne des Échéances (Leases)',
            defaults={
                'func': 'core.tasks.generer_leases_quotidien_task',
                'schedule_type': Schedule.CRON,
                'cron': '0 2 * * *',
                'repeats': -1,
            }
        )
        logger.info("✅ Tâche CRON 'Génération des Échéances' planifiée pour 02:00 AM.")

    except Exception as e:
        logger.warning(f"❌ Impossible de configurer les tâches planifiées (Recouvrement) : {e}")


class RecouvrementConfig(AppConfig):
    name = 'recouvrement'

    def ready(self):
        post_migrate.connect(setup_scheduled_tasks, sender=self)