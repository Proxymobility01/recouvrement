import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django_q.models import Schedule
from .models import ReglePenalite
from django.db import transaction as db_transaction

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ReglePenalite)
def synchroniser_schedule_django_q(sender, instance, created, **kwargs):
    nom_tache = f"regle_penalite_{instance.id}"

    try:
        if instance.occurrences == 0:
            Schedule.objects.filter(name=nom_tache).delete()
            logger.info(f"Règle '{instance.nom}' désactivée. Tâche supprimée.")
            return

        schedule_kwargs = {
            'func': 'core.tasks.appliquer_penalite_task',
            'kwargs': {'regle_id': instance.id},
            'schedule_type': instance.frequence,
            'repeats': -1,
            'next_run': instance.debut,
        }
        if instance.frequence == Schedule.CRON and instance.cron_expression:
            schedule_kwargs['cron'] = instance.cron_expression

        with db_transaction.atomic():
            Schedule.objects.update_or_create(
                name=nom_tache,
                defaults=schedule_kwargs,
            )
        logger.info(f"Tâche planifiée/mise à jour pour la règle '{instance.nom}'.")

    except Exception as e:
        logger.exception(
            f"Erreur lors de la synchronisation de la règle {instance.id} : {e}"
        )


@receiver(post_delete, sender=ReglePenalite)
def nettoyer_schedule_lors_de_la_suppression(sender, instance, **kwargs):
    """
    Si une règle est supprimée de la base de données, on détruit immédiatement
    la tâche Django-Q associée pour éviter les exécutions fantômes.
    """
    nom_tache = f"regle_penalite_{instance.id}"

    # Suppression définitive de la tâche planifiée
    deleted_count, _ = Schedule.objects.filter(name=nom_tache).delete()

    if deleted_count > 0:
        logger.info(f"🗑️ [Django-Q] Tâche fantôme '{nom_tache}' supprimée suite à la destruction de la règle.")