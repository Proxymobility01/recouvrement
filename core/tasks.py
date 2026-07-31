import logging
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.db import transaction, DatabaseError
from django.db.models import Count, F
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_q.tasks import async_task, schedule
from django_q.models import Schedule

from core.utils import notifier_utilisateur, remove_accents
from recouvrement.models import (
    SessionPaiement,
    Paiement,
    Lease,
    Contrat,
    ReglePenalite,
    Penalite,
    RegleGenerationLease,
)
from recouvrement.services import (
    PaymentService,
    assurer_lease_suivant_du_lease,
    generer_leases_pour_regle,
    normaliser_limite_generation,
)
from statistiques.services import statistiques_du_jour

logger = logging.getLogger(__name__)


def paiement_task(session_id, statut_gateway):
    """
    Tâche asynchrone Django Q2 pour ventiler les lignes de paiement.
    """
    logger.info(f"[PaiementTask] Lancement de la tâche pour la session ID: {session_id} avec statut: {statut_gateway}")

    try:
        # 🚀 CORRECTION 1 : On précharge l'utilisateur pour éviter une requête SQL inutile plus tard
        session = SessionPaiement.objects.select_related('utilisateur').get(id=session_id)

        with transaction.atomic():
            leases_payes_ids = set()
            lignes_paiement = Paiement.objects.select_for_update().filter(
                session=session,
                compte_id=session.compte_id,
                statut=Paiement.STATUT_EN_ATTENTE
            ).order_by('id')

            # 🚀 CORRECTION 2 : On ne fait plus de "return", on exécute la ventilation
            # UNIQUEMENT s'il y a des lignes. Mais on laissera le code continuer ensuite vers le SSE.
            if lignes_paiement.exists():

                # ==========================================
                # SCÉNARIO A : PAIEMENT RÉUSSI
                # ==========================================
                if statut_gateway == 'SUCCESS':
                    for paiement in lignes_paiement:
                        paiement.statut = Paiement.STATUT_VALIDE
                        paiement.date_paiement = session.date_validation or timezone.now()
                        paiement.save()

                        if paiement.lease_id:
                            lease = (
                                Lease.objects.select_for_update()
                                .get(
                                    pk=paiement.lease_id,
                                    compte_id=session.compte_id,
                                )
                            )
                            ancien_statut_lease = lease.statut
                            lease.montant_paye += paiement.montant
                            lease.statut = Lease.STATUT_PAYE if lease.montant_paye >= lease.montant_attendu else Lease.STATUT_PARTIEL
                            lease.save()

                            contrat = (
                                Contrat.objects.select_for_update()
                                .get(
                                    pk=lease.contrat_id,
                                    compte_id=session.compte_id,
                                )
                            )
                            contrat.montant_restant = max(contrat.montant_restant - paiement.montant, Decimal('0.00'))
                            contrat.montant_paye += paiement.montant

                            if contrat.montant_restant == 0:
                                contrat.statut = Contrat.STATUT_SOLDE
                            contrat.save()

                            if (
                                paiement.methode == Paiement.METHODE_MOBILE_MONEY
                                and ancien_statut_lease != Lease.STATUT_PAYE
                                and lease.statut == Lease.STATUT_PAYE
                            ):
                                leases_payes_ids.add(lease.id)

                    leases_payes_ordonnes = (
                        Lease.objects.filter(id__in=leases_payes_ids)
                        .order_by('date_echeance', 'id')
                        .values_list('id', flat=True)
                    )
                    for lease_paye_id in leases_payes_ordonnes:
                        try:
                            resultat_generation = (
                                assurer_lease_suivant_du_lease(
                                    lease_paye_id
                                )
                            )
                            logger.info(
                                "[PaiementTask] Lease source=%s : "
                                "génération suivante=%s, lease=%s.",
                                lease_paye_id,
                                resultat_generation['statut'],
                                resultat_generation['lease_id'],
                            )
                        except Exception:
                            # Le paiement Mobile Money reste comptabilisé. La
                            # règle planifiée pourra générer l'occurrence si
                            # cette tentative immédiate échoue.
                            logger.exception(
                                "[PaiementTask] Échec de la génération "
                                "suivant le lease payé %s.",
                                lease_paye_id,
                            )

                    logger.info(f"[PaiementTask] ✅ Ventilation SUCCESS terminée pour {session.reference}.")

                # ==========================================
                # SCÉNARIO B : ÉCHEC OU ANNULATION
                # ==========================================
                elif statut_gateway in ['FAILED', 'CANCELED']:
                    nouveau_statut = Paiement.STATUT_ECHEC if statut_gateway == 'FAILED' else Paiement.STATUT_ANNULE
                    lignes_paiement.update(statut=nouveau_statut)
                    logger.info(f"[PaiementTask] ❌ Lignes passées en {nouveau_statut} pour {session.reference}.")
            else:
                logger.info(f"[PaiementTask] Session {session.reference} déjà ventilée. On passe direct au SSE.")

        # =========================================================
        # 🚀 ENVOI DU SSE (Garantit d'être exécuté quoi qu'il arrive)
        # =========================================================
        payload_paygate = session.webhook_payload or {}
        failure_reason = payload_paygate.get("failure_reason", "Une erreur est survenue lors du traitement.")

        sse_data = {
            "session_id": session.id,
            "reference": session.reference,
            "statut": statut_gateway,
            "montant": str(session.montant_total),
            "message": "Votre paiement a été validé avec succès !" if statut_gateway == "SUCCESS" else f"Échec du paiement : {failure_reason}"
        }

        if hasattr(session, 'utilisateur') and session.utilisateur:
            notifier_utilisateur(
                compte_id=session.compte_id,
                user_id=session.utilisateur.id,
                event_type="transaction.completed",
                data=sse_data
            )
            logger.info(f"[PaiementTask] 📣 Notification SSE envoyée à l'utilisateur {session.utilisateur.id}")

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
        session = (
            SessionPaiement.objects
            .select_related('config_paiement')
            .get(id=session_id)
        )
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
        config_paiement = PaymentService.config_paiement_pour_session(
            session
        )
        payload = PaymentService.verifier_statut_transaction(
            config_paiement_id=config_paiement.id,
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
                'core.tasks.paiement_task',
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
        'core.tasks.verifier_statut_session_task',
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


def generer_leases_task(regle_id, jusqu_a=None):
    """
    Tâche Q2 appelée par le schedule propre à une RegleGenerationLease.

    ``jusqu_a`` est optionnel et sert surtout aux tests ou aux relances
    explicites. En fonctionnement normal, l'instant courant est la limite.
    """
    limite = normaliser_limite_generation(jusqu_a)
    logger.info(
        "[GenererLeasesTask] Démarrage règle=%s jusqu_a=%s.",
        regle_id,
        limite.isoformat(),
    )

    try:
        resultat = generer_leases_pour_regle(
            regle_id=regle_id,
            jusqu_a=limite,
        )
    except RegleGenerationLease.DoesNotExist:
        logger.warning(
            "[GenererLeasesTask] Règle %s introuvable. Tâche ignorée.",
            regle_id,
        )
        return {
            'regle_id': regle_id,
            'statut': 'REGLE_INTROUVABLE',
        }
    except Exception:
        logger.exception(
            "[GenererLeasesTask] Échec critique pour la règle %s.",
            regle_id,
        )
        raise

    logger.info(
        "[GenererLeasesTask] Règle %s terminée : %s lease(s) créé(s), "
        "%s erreur(s).",
        regle_id,
        resultat['leases_crees'],
        resultat['erreurs'],
    )
    return resultat


def appliquer_penalite_task(regle_id):

    logger.info(f"[Pénalités] Démarrage de l'évaluation pour la règle ID: {regle_id}")

    try:
        regle = ReglePenalite.objects.get(id=regle_id)
    except ReglePenalite.DoesNotExist:
        logger.error(f"[Pénalités] Règle {regle_id} introuvable en base. Arrêt de la tâche.")
        return

    # Sécurité : règle désactivée (occurrences=0)
    if regle.occurrences == 0:
        logger.info(f"[Pénalités] La règle '{regle.nom}' est désactivée (occurrences=0). Arrêt.")
        return

    aujourdhui = timezone.now().date()

    # ================================================================
    # ÉTAPE 1 : FILTRAGE ET COMPTAGE (Sans verrou)
    # ================================================================
    leases_cibles = Lease.objects.filter(
        compte_id=regle.compte_id,
        contrat__regle_penalite=regle,
        statut__in=[Lease.STATUT_NON_PAYE, Lease.STATUT_PARTIEL],
        date_echeance__date__lte=aujourdhui,
    ).exclude(
        contrat__statut__in=['SUSPENDU', 'CONTENTIEUX', 'SOLDE']
    ).annotate(
        nb_penalites_existantes=Count('penalites')
    )

    # Plafond par lease : on exclut ceux qui ont déjà atteint leur quota
    if regle.occurrences > -1:
        leases_cibles = leases_cibles.filter(
            nb_penalites_existantes__lt=regle.occurrences
        )

    # 🚀 ASTUCE : On extrait les résultats dans un dictionnaire en mémoire.
    # Format : {id_du_lease: nombre_de_penalites}
    lease_data = dict(leases_cibles.values_list('id', 'nb_penalites_existantes'))

    if not lease_data:
        logger.info(
            f"[Pénalités] Règle '{regle.nom}' : "
            "aucun bail en retard éligible ou tous les plafonds atteints."
        )
        return

    # ================================================================
    # ÉTAPE 2 : VERROUILLAGE ET PRÉPARATION (Sans aggregation/GROUP BY)
    # ================================================================
    penalites_a_creer = []
    lease_ids = []

    with transaction.atomic():
        # 🚀 CORRECTION : On refait la requête juste avec les IDs, ce qui
        # permet à PostgreSQL d'appliquer le verrou sans lever d'erreur.
        leases_a_traiter = list(
            Lease.objects.select_related('contrat')
            .filter(id__in=lease_data.keys())
            .select_for_update(of=('self', 'contrat'))
        )

        for lease in leases_a_traiter:
            contrat = lease.contrat

            # On récupère le nombre de pénalités calculé à l'étape 1
            numero_occurrence = lease_data[lease.id] + 1
            limite = "∞" if regle.occurrences == -1 else str(regle.occurrences)
            date_str = lease.date_echeance.strftime('%d/%m/%Y')
            nom_nettoye = (
                remove_accents(contrat.nom_complet).lower()
                if contrat.nom_complet else ""
            )

            penalites_a_creer.append(Penalite(
                compte_id=contrat.compte_id,
                lease=lease,
                nom_complet=contrat.nom_complet,
                nom_complet_search=nom_nettoye,
                statut=Penalite.STATUT_NON_PAYE,
                montant=regle.montant,
                motif=f"Paiement manqué pour la date du {date_str} ({numero_occurrence}/{limite})",
                date_application=timezone.now(),
            ))
            lease_ids.append(lease.id)

        # ================================================================
        # ÉTAPE 3 : INSERTION ET MISE À JOUR EN MASSE
        # ================================================================

        # 3a. Insertion des pénalités
        if penalites_a_creer:
            Penalite.objects.bulk_create(penalites_a_creer)

        # 3b. Mise à jour des leases (chaque lease reçoit exactement regle.montant)
        if lease_ids:
            Lease.objects.filter(id__in=lease_ids).update(
                montant_attendu=F('montant_attendu') + regle.montant
            )

        # 3c. Mise à jour des contrats
        penalites_par_contrat = Counter(l.contrat_id for l in leases_a_traiter)

        for contrat_id, nb_leases in penalites_par_contrat.items():
            montant_a_ajouter = regle.montant * nb_leases
            Contrat.objects.filter(id=contrat_id).update(
                montant_total=F('montant_total') + montant_a_ajouter,
                montant_restant=F('montant_restant') + montant_a_ajouter,
            )

    logger.info(
        f"[Pénalités] Règle '{regle.nom}' exécutée avec succès : "
        f"{len(penalites_a_creer)} pénalité(s) générée(s) "
        f"sur {len(penalites_par_contrat)} contrat(s)."
    )
