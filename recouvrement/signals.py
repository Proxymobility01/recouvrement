import logging
from django.db.models.signals import post_save, post_delete, pre_save
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


@receiver(pre_save, sender=RegleGenerationLease)
def detecter_modification_planification_generation_lease(
    sender,
    instance,
    **kwargs,
):
    """
    Mémorise si la planification a réellement changé.

    Une modification purement descriptive (par exemple le nom) ne doit pas
    replacer ``next_run`` à la date de début et provoquer un rattrapage
    involontaire.
    """
    if not instance.pk:
        instance._planification_modifiee = True
        return

    ancienne_planification = (
        sender.objects
        .filter(pk=instance.pk)
        .values('frequence', 'cron_expression', 'debut', 'actif')
        .first()
    )
    if ancienne_planification is None:
        instance._planification_modifiee = True
        return

    instance._planification_modifiee = any(
        ancienne_planification[champ] != getattr(instance, champ)
        for champ in (
            'frequence',
            'cron_expression',
            'debut',
            'actif',
        )
    )


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

        schedule_existe = Schedule.objects.filter(name=nom_tache).exists()

        # 2. Configuration des paramètres de la tâche
        schedule_kwargs = {
            'func': 'core.tasks.generer_leases_task',
            'kwargs': {'regle_id': instance.id},
            'schedule_type': instance.frequence,
            'repeats': 1 if instance.frequence == Schedule.ONCE else -1,
            'cron': (
                instance.cron_expression
                if instance.frequence == Schedule.CRON
                else None
            ),
        }

        planification_a_reinitialiser = (
            created
            or not schedule_existe
            or getattr(instance, '_planification_modifiee', False)
        )
        if planification_a_reinitialiser:
            schedule_kwargs['next_run'] = instance.debut

        # 3. Enregistrement en base de données
        with db_transaction.atomic():
            schedule, _ = Schedule.objects.update_or_create(
                name=nom_tache,
                defaults=schedule_kwargs,
            )
            # À la création d'un CRON, Schedule.save() recalcule next_run
            # depuis l'instant présent. On restaure donc explicitement la
            # première exécution choisie sur la règle.
            if (
                planification_a_reinitialiser
                and schedule.next_run != instance.debut
            ):
                Schedule.objects.filter(pk=schedule.pk).update(
                    next_run=instance.debut,
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
