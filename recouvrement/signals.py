import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django_q.models import Schedule
from .models import ReglePenalite, RegleGenerationLease
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


@receiver(post_save, sender=RegleGenerationLease)
def synchroniser_schedule_generation_lease(sender, instance, created, **kwargs):
    """
    Crée, met à jour ou supprime la tâche Django-Q associée à la règle de génération de lease.
    """
    nom_tache = f"regle_generation_lease_{instance.id}"

    try:
        # 1. Si la règle est désactivée par un agent, on supprime la tâche CRON
        if not instance.actif:
            Schedule.objects.filter(name=nom_tache).delete()
            logger.info(f"Règle de génération '{instance.nom}' désactivée. Tâche supprimée.")
            return

        # 2. Configuration des paramètres de la tâche
        schedule_kwargs = {
            'func': 'core.tasks.generer_leases_task',
            'kwargs': {'regle_id': instance.id},
            'schedule_type': instance.frequence,
            'repeats': -1,
            'next_run': instance.debut,
        }

        # 3. Ajout de l'expression CRON si applicable
        if instance.frequence == Schedule.CRON and instance.cron_expression:
            schedule_kwargs['cron'] = instance.cron_expression

        # 4. Enregistrement en base de données
        with db_transaction.atomic():
            Schedule.objects.update_or_create(
                name=nom_tache,
                defaults=schedule_kwargs,
            )
        logger.info(f"Tâche planifiée/mise à jour pour la règle '{instance.nom}'.")

    except Exception as e:
        logger.exception(
            f"Erreur lors de la synchronisation de la règle de génération {instance.id} : {e}"
        )


@receiver(post_delete, sender=RegleGenerationLease)
def nettoyer_schedule_generation_lors_de_la_suppression(sender, instance, **kwargs):
    """
    Si une règle est supprimée de la base de données, on détruit immédiatement
    la tâche Django-Q associée pour éviter les exécutions fantômes.
    """
    nom_tache = f"regle_generation_lease_{instance.id}"

    # Suppression définitive de la tâche planifiée
    deleted_count, _ = Schedule.objects.filter(name=nom_tache).delete()

    if deleted_count > 0:
        logger.info(
            f"🗑️ [Django-Q] Tâche fantôme '{nom_tache}' supprimée suite à la destruction de la règle de génération.")