from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .models import SessionPaiement, Paiement, Lease, Contrat
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=SessionPaiement)
def traitement_post_session(sender, instance, created, **kwargs):
    """
    Signal déclenché chaque fois qu'une SessionPaiement est sauvegardée via .save().
    Il s'occupe de répercuter le statut sur la comptabilité (Paiements, Leases, Contrats).
    """
    # Si la session vient tout juste d'être initiée (création), on ne fait rien
    if created:
        return

    # --- CAS 1 : SUCCÈS (La session est validée par PayGate) ---
    if instance.statut == SessionPaiement.STATUT_VALIDE:
        with transaction.atomic():
            # 1. MISE À JOUR RAPIDE DES LIGNES DE PAIEMENT (Pour la base de données brute/BI)
            # On utilise .update() pour ne faire qu'une seule requête SQL pour toutes les lignes
            lignes_a_mettre_a_jour = instance.lignes_paiement.exclude(statut=Paiement.STATUT_VALIDE)
            lignes_a_mettre_a_jour.update(
                statut=Paiement.STATUT_VALIDE,
                date_paiement=instance.date_validation,
            )

            # 2. MISE À JOUR DES ÉCHÉANCES (Leases) ET PRÉPARATION DES CONTRATS
            # On précharge les relations pour éviter les requêtes SQL en boucle
            lignes = instance.lignes_paiement.select_related('lease', 'contrat').all()

            # 🚀 AMÉLIORATION 1 : Dictionnaire pour gérer les paiements sur de multiples contrats
            montants_par_contrat = {}

            for paiement in lignes:
                lease = paiement.lease
                if lease:
                    # On ajoute l'argent à l'échéance
                    lease.montant_paye += paiement.montant

                    # On recalcule le statut de l'échéance
                    if lease.montant_paye >= lease.montant_attendu:
                        lease.statut = Lease.STATUT_PAYE
                    else:
                        lease.statut = Lease.STATUT_PARTIEL

                    lease.save(update_fields=['montant_paye', 'statut'])

                # On cumule le montant par contrat (Moto, GPS, etc.)
                contrat_id = paiement.contrat_id
                if contrat_id not in montants_par_contrat:
                    montants_par_contrat[contrat_id] = Decimal('0.00')

                montants_par_contrat[contrat_id] += paiement.montant

            # 3. MISE À JOUR SÉCURISÉE DES CONTRATS
            for contrat_id, montant_a_deduire in montants_par_contrat.items():
                if montant_a_deduire > 0:
                    # 🚀 AMÉLIORATION 2 : select_for_update() verrouille la ligne du contrat
                    # empêchant toute Race Condition si deux webhooks arrivent en même temps.
                    contrat_verrouille = Contrat.objects.select_for_update().get(id=contrat_id)

                    nouveau_reste = contrat_verrouille.montant_restant - montant_a_deduire
                    contrat_verrouille.montant_restant = max(nouveau_reste, Decimal('0.00'))

                    if contrat_verrouille.montant_restant == 0:
                        contrat_verrouille.statut = Contrat.STATUT_SOLDE

                    contrat_verrouille.save(update_fields=['montant_restant', 'statut'])

            logger.info(f"[Signal] Comptabilité mise à jour pour la session {instance.reference}")

    # --- CAS 2 : ÉCHEC (La session est refusée par PayGate) ---
    elif instance.statut == SessionPaiement.STATUT_ECHEC:
        with transaction.atomic():
            # 1 seule requête SQL pour basculer toutes les lignes en statut ECHEC
            instance.lignes_paiement.exclude(statut=Paiement.STATUT_ECHEC).update(
                statut=Paiement.STATUT_ECHEC
            )
            logger.info(f"[Signal] Lignes passées en ÉCHEC pour la session {instance.reference}")