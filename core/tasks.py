import logging
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.db import transaction, DatabaseError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_q.tasks import async_task, schedule
from django_q.models import Schedule

from recouvrement.models import SessionPaiement, Paiement, Lease, Contrat
from recouvrement.services import PaymentService
from statistiques.services import statistiques_du_jour

logger = logging.getLogger(__name__)


def paiement_task(session_id, statut_gateway):
    """
    Tâche asynchrone Django Q2 pour ventiler les lignes de paiement.
    - SUCCESS : Met à jour Paiement, Lease et Contrat.
    - FAILED / CANCELED : Met à jour uniquement Paiement.
    """
    logger.info(f"[PaiementTask] Lancement de la tâche pour la session ID: {session_id} avec statut: {statut_gateway}")

    try:
        session = SessionPaiement.objects.get(id=session_id)

        with transaction.atomic():
            # 1. On verrouille uniquement les paiements enfants qui sont encore EN_ATTENTE
            lignes_paiement = Paiement.objects.select_for_update().filter(
                session=session,
                statut=Paiement.STATUT_EN_ATTENTE
            )

            if not lignes_paiement.exists():
                logger.info(f"[PaiementTask] Aucune ligne en attente pour la session {session.reference}. Arrêt.")
                return

            # ==========================================
            # SCÉNARIO A : PAIEMENT RÉUSSI
            # ==========================================
            if statut_gateway == 'SUCCESS':
                for paiement in lignes_paiement:
                    # A. Mise à jour de la ligne de Paiement
                    paiement.statut = Paiement.STATUT_VALIDE
                    paiement.date_paiement = session.date_validation or timezone.now()
                    paiement.save()

                    # B. Mise à jour du Lease (L'échéance)
                    lease = paiement.lease
                    if lease:
                        lease.montant_paye += paiement.montant
                        lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                        lease.save()

                        # C. Mise à jour du Contrat (La dette globale)
                        contrat = lease.contrat
                        contrat.montant_restant = max(contrat.montant_restant - paiement.montant, Decimal('0.00'))
                        contrat.montant_paye += paiement.montant

                        if contrat.montant_restant == 0:
                            contrat.statut = Contrat.STATUT_SOLDE
                        contrat.save()

                logger.info(f"[PaiementTask] ✅ Ventilation SUCCESS terminée pour {session.reference}.")

            # ==========================================
            # SCÉNARIO B : ÉCHEC OU ANNULATION
            # ==========================================
            elif statut_gateway in ['FAILED', 'CANCELED']:
                nouveau_statut = Paiement.STATUT_ECHEC if statut_gateway == 'FAILED' else Paiement.STATUT_ANNULE
                lignes_paiement.update(statut=nouveau_statut)
                logger.info(f"[PaiementTask] ❌ Lignes passées en {nouveau_statut} pour {session.reference}.")

    except SessionPaiement.DoesNotExist:
        logger.error(f"[PaiementTask] Erreur : Session ID {session_id} introuvable en BDD.")
    except DatabaseError:
        logger.exception(f"[PaiementTask] Erreur base de données lors de la ventilation de la session {session_id}.")
        raise
    except Exception:
        logger.exception(f"[PaiementTask] Erreur critique inattendue sur la session {session_id}.")
        raise


def verifier_statut_session_task(session_id: int):
    """
    Vérifie activement le statut de la session de paiement.
    Si terminé : Met à jour, lance la ventilation et s'arrête.
    Si non terminé : Relance un schedule dynamique et s'arrête.
    """
    logger.info(f"[VerifierStatutSessionTask] Début de la vérification pour la Session ID [{session_id}]")

    try:
        session = SessionPaiement.objects.get(id=session_id)
    except SessionPaiement.DoesNotExist:
        logger.error(f"[VerifierStatutSessionTask] Erreur : Session ID [{session_id}] introuvable en BDD. Arrêt.")
        return None

    if session.statut != SessionPaiement.STATUT_EN_ATTENTE:
        logger.info(
            f"[VerifierStatutSessionTask] Session [{session.reference}] déjà traitée (Statut: {session.statut}). Arrêt.")
        return str(session.id)

    # ==========================================
    # 1. 📞 APPEL À L'AGRÉGATEUR (PayGate)
    # ==========================================
    try:
        payload = PaymentService.verifier_statut_transaction(
            compte_id=session.compte_id,
            gateway_reference=session.gateway_reference
        )

        statut_gateway = str(payload.get('status', '')).upper()

        if statut_gateway in ['SUCCESS', 'FAILED', 'CANCELED']:
            logger.info(
                f"[VerifierStatutSessionTask] État terminal atteint ({statut_gateway}) pour [{session.reference}].")

            if statut_gateway == 'SUCCESS':
                session.statut = SessionPaiement.STATUT_VALIDE
                confirmed_at_str = payload.get('confirmed_at')
                if confirmed_at_str:
                    parsed_date = parse_datetime(confirmed_at_str)
                    session.date_validation = parsed_date if parsed_date else timezone.now()
                else:
                    session.date_validation = timezone.now()

            elif statut_gateway == 'FAILED':
                session.statut = SessionPaiement.STATUT_ECHEC
            elif statut_gateway == 'CANCELED':
                session.statut = SessionPaiement.STATUT_ANNULE

            session.webhook_payload = payload
            session.save()

            # 🚀 DÉLÉGATION À LA VENTILATION COMPTABLE
            async_task(
                'recouvrement.tasks.paiement_task',
                session.id,
                statut_gateway
            )

            return str(session.id)

    except Exception:
        # En cas de panne réseau avec PayGate, on attrape l'erreur silencieusement
        logger.warning(
            f"[VerifierStatutSessionTask] Panne réseau/temporaire avec la passerelle pour [{session.reference}]. On maintient PENDING.")

    # ==========================================
    # 2. ⏳ GESTION DES RELANCES (Polling Dynamique)
    # ==========================================
    elapsed_seconds = (timezone.now() - session.created_at).total_seconds()

    if elapsed_seconds >= 300:
        logger.warning(
            f"[VerifierStatutSessionTask] ⏱️ TIMEOUT (Limite des 5 min atteinte) pour [{session.reference}]. "
            "Arrêt définitif des relances. La transaction reste PENDING."
        )
        return None

    delay_seconds = 30 if elapsed_seconds < 120 else 60

    try:
        _schedule_next_verification(session, delay_seconds)
        logger.info(
            f"[VerifierStatutSessionTask] Toujours en attente. Relance planifiée dans {delay_seconds}s pour [{session.reference}].")
    except Exception:
        logger.exception(f"[VerifierStatutSessionTask] Erreur critique de replanification pour [{session.reference}].")

    return None


def _schedule_next_verification(session: SessionPaiement, delay_seconds: int):
    """
    Crée OU remplace le schedule de vérification pour cette Session.
    """
    schedule_name = f"verify_session_{session.id}"
    Schedule.objects.filter(name=schedule_name).delete()

    schedule(
        'recouvrement.tasks.verifier_statut_session_task',
        session.id,
        name=schedule_name,
        schedule_type=Schedule.ONCE,
        next_run=timezone.now() + timedelta(seconds=delay_seconds)
    )


def rafraichir_statistiques_horaire_task():
    """
    Tâche récurrente Django Q2 lancée toutes les heures.
    Recalcule les statistiques de l'entreprise pour la journée en cours.
    """
    aujourdhui = timezone.now().date()
    logger.info(f"[RafraichirStatistiquesHoraireTask] Début du rafraîchissement pour le {aujourdhui}.")

    try:
        # 🚀 CORRECTION : Appel direct de la fonction, pas de call_command
        statistiques_du_jour(date_cible=aujourdhui)
        logger.info(f"[RafraichirStatistiquesHoraireTask] ✅ Agrégation horaire terminée.")

    except Exception:  # 🚀 CORRECTION : Pas de parenthèses à Exception
        logger.exception(f"[RafraichirStatistiquesHoraireTask] ❌ Échec critique lors du calcul.")
        raise


def generer_leases_quotidien_task():
    """
    Tâche récurrente lancée tous les jours à 2h00 du matin.
    Appelle la commande d'administration pour générer les nouvelles échéances.
    """
    logger.info("[GenererLeasesQuotidienTask] ⏳ Démarrage de la génération des échéances.")

    try:
        # 🚀 Utilisation de call_command en passant le nom du fichier (sans .py)
        # Remplace 'generer_leases' par le vrai nom de ton fichier dans management/commands/
        call_command('generer_leases')

        logger.info("[GenererLeasesQuotidienTask] ✅ Génération des échéances terminée avec succès.")

    except Exception:
        logger.exception("[GenererLeasesQuotidienTask] ❌ Échec critique lors de la génération des échéances.")
        raise