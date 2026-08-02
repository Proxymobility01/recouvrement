import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import ceil

import requests
from croniter import croniter
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_q.models import Schedule
from dateutil.relativedelta import relativedelta

from accounts.models import ConfigPaiement
from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from core.utils import format_phone_cm

logger = logging.getLogger(__name__)


class AnnulationLeaseError(Exception):
    """Échéance non annulable (déjà payée, partiellement payée ou déjà annulée)."""


class LeaseGenerationConfigurationError(Exception):
    """Configuration invalide d'une règle de génération de leases."""


def normaliser_limite_generation(valeur=None, fin_de_jour=False):
    """
    Retourne une limite timezone-aware.

    - ``None`` utilise l'instant courant.
    - un datetime ISO conserve son heure ;
    - une date ISO est interprétée à minuit ou à la fin de la journée.
    """
    if valeur is None:
        limite = timezone.now()
    elif isinstance(valeur, datetime):
        limite = valeur
    elif isinstance(valeur, date):
        limite = datetime.combine(valeur, time.max if fin_de_jour else time.min)
    elif isinstance(valeur, str):
        limite = parse_datetime(valeur)
        if limite is None:
            date_parsee = parse_date(valeur)
            if date_parsee is None:
                raise ValueError(
                    "Date invalide. Utilisez YYYY-MM-DD ou un datetime ISO 8601."
                )
            limite = datetime.combine(
                date_parsee,
                time.max if fin_de_jour else time.min,
            )
    else:
        raise TypeError("La limite doit être une date, un datetime, une chaîne ISO ou None.")

    if timezone.is_naive(limite):
        limite = timezone.make_aware(limite, timezone.get_current_timezone())
    return limite


def calculer_prochaine_occurrence(regle, occurrence):
    """
    Calcule l'occurrence qui suit ``occurrence`` à partir de la règle métier.

    Le calcul part toujours de l'occurrence planifiée, et non de l'heure réelle
    d'exécution du worker. Une tâche retardée ne décale donc jamais la cadence.
    """
    occurrence = normaliser_limite_generation(occurrence)
    occurrence_locale = timezone.localtime(
        occurrence,
        timezone.get_current_timezone(),
    )

    if regle.frequence == Schedule.ONCE:
        return None
    if regle.frequence == Schedule.HOURLY:
        suivante = occurrence_locale + timedelta(hours=1)
    elif regle.frequence == Schedule.DAILY:
        suivante = occurrence_locale + timedelta(days=1)
    elif regle.frequence == Schedule.WEEKLY:
        suivante = occurrence_locale + timedelta(weeks=1)
    elif regle.frequence == Schedule.MONTHLY:
        suivante = occurrence_locale + relativedelta(months=1)
    elif regle.frequence == Schedule.CRON:
        if not regle.cron_expression:
            raise LeaseGenerationConfigurationError(
                f"La règle {regle.id} est de type CRON sans expression Cron."
            )
        try:
            suivante = croniter(
                regle.cron_expression,
                occurrence_locale,
            ).get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise LeaseGenerationConfigurationError(
                f"Expression Cron invalide pour la règle {regle.id}: "
                f"{regle.cron_expression}"
            ) from exc
    else:
        raise LeaseGenerationConfigurationError(
            f"Fréquence inconnue pour la règle {regle.id}: {regle.frequence}"
        )

    suivante = normaliser_limite_generation(suivante)
    if suivante <= occurrence:
        raise LeaseGenerationConfigurationError(
            f"La règle {regle.id} ne produit pas une occurrence strictement future."
        )
    return suivante


def premiere_occurrence_planifiee(regle, a_partir_de):
    """Retourne la première occurrence officielle >= ``a_partir_de``.

    Contrairement à :func:`calculer_prochaine_occurrence`, ce calcul part de
    l'ancre de la règle (``debut`` ou l'expression Cron). Une heure arbitraire
    enregistrée sur un contrat ne peut donc jamais devenir l'heure d'un lease.
    """
    fuseau = timezone.get_current_timezone()
    a_partir_de = timezone.localtime(
        normaliser_limite_generation(a_partir_de),
        fuseau,
    )
    debut = timezone.localtime(
        normaliser_limite_generation(regle.debut),
        fuseau,
    )
    point_depart = max(a_partir_de, debut)

    if regle.frequence == Schedule.ONCE:
        return debut if debut >= a_partir_de else None

    if regle.frequence == Schedule.CRON:
        if not regle.cron_expression:
            raise LeaseGenerationConfigurationError(
                f"La règle {regle.id} est de type CRON sans expression Cron."
            )
        try:
            occurrence = croniter(
                regle.cron_expression,
                point_depart - timedelta(microseconds=1),
            ).get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise LeaseGenerationConfigurationError(
                f"Expression Cron invalide pour la règle {regle.id}: "
                f"{regle.cron_expression}"
            ) from exc
        return normaliser_limite_generation(occurrence)

    if point_depart <= debut:
        return debut

    secondes_ecoulees = (point_depart - debut).total_seconds()
    if regle.frequence == Schedule.HOURLY:
        nombre_pas = ceil(secondes_ecoulees / 3600)
        occurrence = debut + timedelta(hours=nombre_pas)
    elif regle.frequence == Schedule.DAILY:
        nombre_pas = ceil(secondes_ecoulees / 86400)
        occurrence = debut + timedelta(days=nombre_pas)
    elif regle.frequence == Schedule.WEEKLY:
        nombre_pas = ceil(secondes_ecoulees / (7 * 86400))
        occurrence = debut + timedelta(weeks=nombre_pas)
    elif regle.frequence == Schedule.MONTHLY:
        occurrence = debut
        while occurrence < point_depart:
            occurrence = occurrence + relativedelta(months=1)
    else:
        raise LeaseGenerationConfigurationError(
            f"Fréquence inconnue pour la règle {regle.id}: {regle.frequence}"
        )

    return normaliser_limite_generation(occurrence)


def normaliser_curseur_generation(regle, curseur):
    """Aligne un curseur exigible sur les créneaux officiels de sa date.

    La date locale du curseur reste la borne métier. Son heure n'est qu'une
    valeur d'éligibilité et n'est jamais reprise dans ``Lease.date_echeance``.
    Repartir du début de cette date permet de rattraper un premier créneau
    manquant avant de traiter les suivants.
    """
    curseur = normaliser_limite_generation(curseur)
    fuseau = timezone.get_current_timezone()
    curseur_local = timezone.localtime(curseur, fuseau)
    debut_jour_naif = datetime.combine(curseur_local.date(), time.min)
    debut_jour = timezone.make_aware(debut_jour_naif, fuseau)
    return premiere_occurrence_planifiee(regle, debut_jour)


def _normaliser_jours_repos(jours_repos):
    jours = {
        int(jour)
        for jour in (jours_repos or [])
        if str(jour).lstrip('-').isdigit() and 0 <= int(jour) <= 6
    }
    # Même garde que l'ancienne commande : une semaine entièrement chômée
    # ne doit pas provoquer une boucle sans fin.
    return set() if len(jours) >= 7 else jours


def generer_leases_pour_regle(regle_id, jusqu_a=None):
    """
    Génère, de façon idempotente, toutes les occurrences exigibles d'une règle.

    Cette fonction est l'unique moteur métier partagé par la commande manuelle
    et par Django Q2. Le ``next_run`` de Q2 déclenche le contrôle ; le curseur
    ``Contrat.prochaine_echeance`` autorise réellement chaque création.
    """
    from .models import Contrat, Lease, Parametre, RegleGenerationLease

    limite = normaliser_limite_generation(jusqu_a)
    regle = RegleGenerationLease.objects.get(pk=regle_id)

    resultat = {
        'regle_id': regle.id,
        'regle': regle.nom,
        'jusqu_a': limite.isoformat(),
        'contrats_cibles': 0,
        'leases_crees': 0,
        'doublons_ignores': 0,
        'occurrences_repos_ignorees': 0,
        'contrats_termines': 0,
        'erreurs': 0,
    }

    if not regle.actif:
        logger.info(
            "[Génération leases] Règle %s inactive : aucun traitement.",
            regle.id,
        )
        return resultat

    jours_repos = _normaliser_jours_repos(
        Parametre.objects.filter(compte_id=regle.compte_id)
        .values_list('jours_repos', flat=True)
        .first()
    )

    contrat_ids = list(
        Contrat.objects.filter(
            compte_id=regle.compte_id,
            regle_generation_id=regle.id,
            statut=Contrat.STATUT_ACTIF,
            prochaine_echeance__isnull=False,
            prochaine_echeance__lte=limite,
        ).values_list('id', flat=True)
    )
    resultat['contrats_cibles'] = len(contrat_ids)

    for contrat_id in contrat_ids:
        compteurs_contrat = {
            'leases_crees': 0,
            'doublons_ignores': 0,
            'occurrences_repos_ignorees': 0,
            'contrats_termines': 0,
        }

        try:
            with transaction.atomic():
                contrat = (
                    Contrat.objects.select_for_update()
                    .select_related('regle_generation')
                    .get(
                        pk=contrat_id,
                        compte_id=regle.compte_id,
                        regle_generation_id=regle.id,
                        statut=Contrat.STATUT_ACTIF,
                    )
                )

                if contrat.prochaine_echeance is None:
                    continue

                # Un autre worker ou la commande a pu faire avancer le curseur
                # pendant que ce worker attendait le verrou. Le test sur la
                # valeur brute reste la seule condition d'éligibilité.
                curseur = normaliser_limite_generation(
                    contrat.prochaine_echeance
                )
                if curseur <= limite:
                    # Une fois le contrat exigible, l'heure du curseur est
                    # remplacée par la première occurrence officielle de sa
                    # date. Les heures arbitraires ne deviennent jamais des
                    # dates d'échéance de lease.
                    occurrence = normaliser_curseur_generation(regle, curseur)
                    contrat.prochaine_echeance = occurrence
                    if occurrence is None:
                        compteurs_contrat['contrats_termines'] = 1

                while (
                    contrat.prochaine_echeance
                    and contrat.prochaine_echeance <= limite
                ):
                    occurrence = contrat.prochaine_echeance

                    occurrence_locale = timezone.localtime(
                        occurrence,
                        timezone.get_current_timezone(),
                    )

                    if (
                        contrat.date_fin
                        and occurrence_locale.date() > contrat.date_fin
                    ):
                        contrat.prochaine_echeance = None
                        compteurs_contrat['contrats_termines'] = 1
                        break

                    occurrence_suivante = calculer_prochaine_occurrence(
                        regle,
                        occurrence,
                    )

                    if occurrence_locale.weekday() in jours_repos:
                        compteurs_contrat['occurrences_repos_ignorees'] += 1
                    else:
                        _, created = Lease.objects.get_or_create(
                            contrat=contrat,
                            date_echeance=occurrence,
                            defaults={
                                'compte_id': contrat.compte_id,
                                'montant_attendu': contrat.montant_par_paiement,
                                'statut': Lease.STATUT_NON_PAYE,
                            },
                        )
                        compteur = (
                            'leases_crees'
                            if created
                            else 'doublons_ignores'
                        )
                        compteurs_contrat[compteur] += 1

                    contrat.prochaine_echeance = occurrence_suivante
                    if occurrence_suivante is None:
                        compteurs_contrat['contrats_termines'] = 1
                        break

                contrat.save(
                    update_fields=['prochaine_echeance', 'updated_at']
                )

        except Contrat.DoesNotExist:
            # État modifié par une autre transaction : ce n'est pas une erreur.
            logger.info(
                "[Génération leases] Contrat %s devenu inéligible.",
                contrat_id,
            )
            continue
        except Exception:
            resultat['erreurs'] += 1
            logger.exception(
                "[Génération leases] Échec pour le contrat %s et la règle %s.",
                contrat_id,
                regle.id,
            )
            continue

        for cle, valeur in compteurs_contrat.items():
            resultat[cle] += valeur

    logger.info(
        "[Génération leases] Règle %s terminée : %s créés, %s doublons, "
        "%s repos ignorés, %s erreurs.",
        regle.id,
        resultat['leases_crees'],
        resultat['doublons_ignores'],
        resultat['occurrences_repos_ignorees'],
        resultat['erreurs'],
    )
    return resultat


def assurer_lease_suivant_du_lease(lease_source_id):
    """
    S'assure qu'un lease postérieur au lease Mobile Money payé existe.

    ``Contrat.prochaine_echeance`` est l'unique curseur de génération. La
    date du lease payé sert seulement à vérifier qu'un lease postérieur
    n'existe pas déjà. Le paiement et la tâche planifiée peuvent appeler
    cette logique en concurrence : le verrou du contrat et la contrainte
    d'unicité du lease garantissent l'idempotence.
    """
    from .models import Contrat, Lease, Parametre

    def ignorer(raison):
        return {
            'statut': 'IGNORE',
            'raison': raison,
            'lease_id': None,
            'lease_cree': False,
        }

    with transaction.atomic():
        lease_source = (
            Lease.objects.select_for_update()
            .get(pk=lease_source_id)
        )
        contrat = (
            Contrat.objects.select_for_update()
            .get(pk=lease_source.contrat_id)
        )

        if lease_source.compte_id != contrat.compte_id:
            return ignorer('INCOHERENCE_COMPTE')
        if lease_source.statut != Lease.STATUT_PAYE:
            return ignorer('LEASE_SOURCE_NON_PAYE')
        if (
            contrat.statut != Contrat.STATUT_ACTIF
            or contrat.montant_restant <= 0
        ):
            return ignorer('CONTRAT_NON_ACTIF_OU_SOLDE')

        regle = contrat.regle_generation
        if regle is None:
            return ignorer('REGLE_GENERATION_ABSENTE')
        if not regle.actif:
            return ignorer('REGLE_GENERATION_INACTIVE')
        if regle.compte_id != contrat.compte_id:
            return ignorer('REGLE_GENERATION_AUTRE_COMPTE')
        if contrat.prochaine_echeance is None:
            return ignorer('PROCHAINE_ECHEANCE_ABSENTE')

        jours_repos = _normaliser_jours_repos(
            Parametre.objects.filter(compte_id=contrat.compte_id)
            .values_list('jours_repos', flat=True)
            .first()
        )

        # La tâche planifiée, un paiement précédent de la même session ou
        # une notification déjà traitée peuvent avoir créé le lease suivant.
        # Dans ce cas, il ne faut pas allonger une seconde fois la chaîne.
        lease_suivant_existant = (
            Lease.objects.filter(
                contrat_id=contrat.id,
                compte_id=contrat.compte_id,
                date_echeance__gt=lease_source.date_echeance,
            )
            .exclude(statut=Lease.STATUT_ANNULE)
            .order_by('date_echeance', 'id')
            .first()
        )
        if lease_suivant_existant is not None:
            return {
                'statut': 'EXISTANT',
                'raison': 'LEASE_SUIVANT_DEJA_PRESENT',
                'lease_id': lease_suivant_existant.id,
                'lease_cree': False,
            }

        occurrence = normaliser_curseur_generation(
            regle,
            contrat.prochaine_echeance,
        )

        while occurrence:
            occurrence_locale = timezone.localtime(
                occurrence,
                timezone.get_current_timezone(),
            )

            if (
                contrat.date_fin
                and occurrence_locale.date() > contrat.date_fin
            ):
                contrat.prochaine_echeance = None
                contrat.save(
                    update_fields=['prochaine_echeance', 'updated_at']
                )
                return ignorer('DATE_FIN_DEPASSEE')

            lease_a_occurrence = Lease.objects.filter(
                contrat_id=contrat.id,
                date_echeance=occurrence,
            ).first()
            occurrence_deja_annulee = (
                lease_a_occurrence is not None
                and lease_a_occurrence.statut == Lease.STATUT_ANNULE
            )
            if (
                occurrence > lease_source.date_echeance
                and occurrence_locale.weekday() not in jours_repos
                and not occurrence_deja_annulee
            ):
                break
            occurrence = calculer_prochaine_occurrence(regle, occurrence)

        if occurrence is None:
            contrat.prochaine_echeance = None
            contrat.save(
                update_fields=['prochaine_echeance', 'updated_at']
            )
            return ignorer('AUCUNE_OCCURRENCE_SUIVANTE')

        lease_suivant, lease_cree = Lease.objects.get_or_create(
            contrat=contrat,
            date_echeance=occurrence,
            defaults={
                'compte_id': contrat.compte_id,
                'montant_attendu': contrat.montant_par_paiement,
                'statut': Lease.STATUT_NON_PAYE,
            },
        )

        contrat.prochaine_echeance = calculer_prochaine_occurrence(
            regle,
            occurrence,
        )
        contrat.save(
            update_fields=['prochaine_echeance', 'updated_at']
        )

        return {
            'statut': 'CREE' if lease_cree else 'EXISTANT',
            'raison': '',
            'lease_id': lease_suivant.id,
            'lease_cree': lease_cree,
        }


def annuler_leases_et_prolonger(leases, jours_a_prolonger):
    """
    Annule les échéances fournies et prolonge la date de fin de chaque contrat
    concerné de `jours_a_prolonger` jours OUVRÉS (les jours de repos configurés
    pour le compte propriétaire du contrat sont sautés).

    `leases` : queryset d'échéances DÉJÀ filtré et autorisé par l'appelant
               (l'API le restreint au tenant, l'admin au périmètre du staff).

    Utilisé à la fois par l'API (ContratViewSet.annuler_leases) et par l'action
    de l'admin, afin que les deux ne divergent jamais.

    Lève AnnulationLeaseError si une échéance n'est pas au statut NON_PAYE.
    """
    from .models import Lease, Parametre, Penalite

    if leases.exclude(statut=Lease.STATUT_NON_PAYE).exists():
        raise AnnulationLeaseError(
            "Impossible d'annuler : certaines échéances ont déjà un paiement ou sont déjà annulées."
        )

    # 1. Regroupement par contrat
    leases_par_contrat = defaultdict(list)
    for lease in leases.select_related('contrat'):
        leases_par_contrat[lease.contrat].append(lease)

    if not leases_par_contrat:
        return {'nb_leases': 0, 'nb_contrats': 0, 'contrats_impactes': []}

    # 2. Jours de repos PAR COMPTE.
    # 🚀 Indispensable : un superadmin peut annuler des échéances de plusieurs
    # entreprises à la fois. On ne peut donc pas se baser sur SON compte_id,
    # il faut les jours de repos du compte propriétaire de chaque contrat.
    comptes_concernes = {contrat.compte_id for contrat in leases_par_contrat}
    jours_repos_par_compte = {
        param.compte_id: param.jours_repos
        for param in Parametre.objects.filter(compte_id__in=comptes_concernes)
    }

    nb_leases = sum(len(liste) for liste in leases_par_contrat.values())
    contrats_impactes = []

    # 3. Exécution atomique
    with transaction.atomic():

        # A. Nettoyage des pénalités non payées adossées à ces échéances
        Penalite.objects.filter(
            lease__in=leases,
            statut=Penalite.STATUT_NON_PAYE
        ).delete()

        # B. Annulation de toutes les échéances d'un coup
        leases.update(statut=Lease.STATUT_ANNULE)

        # C. Prolongation intelligente de CHAQUE contrat
        if jours_a_prolonger > 0:
            for contrat in leases_par_contrat:
                # Le JSON peut contenir des chaînes ("6") : on normalise en entiers,
                # sinon la comparaison avec weekday() échouerait silencieusement.
                jours_repos = {
                    int(jour) for jour in (jours_repos_par_compte.get(contrat.compte_id) or [])
                    if str(jour).lstrip('-').isdigit()
                }

                date_courante = contrat.date_fin

                if len(jours_repos) >= 7:
                    # Tous les jours sont déclarés en repos : on ne saute rien,
                    # sinon la boucle ci-dessous ne se terminerait jamais.
                    # (Même garde que dans la commande generer_leases.)
                    date_courante += timedelta(days=jours_a_prolonger)
                else:
                    jours_ajoutes = 0
                    while jours_ajoutes < jours_a_prolonger:
                        date_courante += timedelta(days=1)
                        if date_courante.weekday() not in jours_repos:
                            jours_ajoutes += 1

                contrat.date_fin = date_courante
                contrat.save(update_fields=['date_fin', 'updated_at'])
                contrats_impactes.append(contrat.reference)

    return {
        'nb_leases': nb_leases,
        'nb_contrats': len(leases_par_contrat),
        'contrats_impactes': contrats_impactes,
    }


class PaymentService:
    """
    Service Multi-Tenant pour les paiements Mobile Money.

    RÈGLES MÉTIER
    -------------
    1. Checkout échoue  → rien n'existe nulle part → ECHEC certain.
    2. Checkout réussit → session créée côté PayGate, référence persistée immédiatement.
    3. Collect échoue   → session PayGate existe mais le push USSD n'est PAS parti
                          → on remonte l'erreur brute de PayGate à l'utilisateur,
                            le statut local reste EN_ATTENTE (peut réessayer).
    4. Collect réussit  → push USSD déclenché → EN_ATTENTE + polling.
    """

    # ------------------------------------------------------------------ #
    # CONFIG (cache Redis 24h)
    # ------------------------------------------------------------------ #
    @classmethod
    def resoudre_config_paiement_pour_leases(cls, leases):
        """
        Détermine l'unique configuration autorisée pour un panier.

        La destination financière provient toujours de la configuration
        explicitement affectée au contrat payé. Le client ne la choisit
        jamais.
        """
        leases = list(leases)
        if not leases:
            raise CustomAPIException(
                resp_code=ErrorCodes.BAD_REQUEST,
                status_code=400,
                dev_message="Aucune échéance n'a été fournie.",
            )

        configurations = {}

        for lease in leases:
            contrat = lease.contrat
            if lease.compte_id != contrat.compte_id:
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message=(
                        f"Incohérence de compte entre l'échéance {lease.id} "
                        f"et son contrat {contrat.id}."
                    ),
                )

            if not contrat.config_paiement_id:
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message=(
                        "Aucune configuration Mobile Money n'est affectée "
                        f"au contrat {contrat.reference}."
                    ),
                )

            config = contrat.config_paiement
            if config.compte_id != contrat.compte_id:
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message=(
                        f"La configuration de paiement du contrat "
                        f"{contrat.reference} appartient à un autre compte."
                    ),
                )
            if not config.actif:
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message=(
                        f"La configuration de paiement affectée au contrat "
                        f"{contrat.reference} est inactive."
                    ),
                )

            configurations[config.id] = config

        if len(configurations) != 1:
            raise CustomAPIException(
                resp_code=ErrorCodes.BAD_REQUEST,
                status_code=400,
                dev_message=(
                    "Les échéances sélectionnées utilisent des "
                    "configurations de paiement différentes. Veuillez "
                    "effectuer deux paiements séparés."
                ),
            )

        return next(iter(configurations.values()))

    @classmethod
    def config_paiement_pour_session(cls, session):
        """
        Retourne la configuration explicitement mémorisée par la session.
        """
        if not session.config_paiement_id:
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=(
                    "La session de paiement ne possède aucune configuration."
                ),
            )

        config = session.config_paiement
        if config.compte_id != session.compte_id:
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=500,
                dev_message=(
                    "La configuration mémorisée par la session appartient "
                    "à un autre compte."
                ),
            )
        return config

    @classmethod
    def _get_config(cls, config_paiement_id, force_refresh=False):
        cache_key = f"credentials_config_{config_paiement_id}"

        if force_refresh:
            cache.delete(cache_key)
            logger.info(
                "Cache de configuration invalidé pour la configuration %s.",
                config_paiement_id,
            )
        else:
            cached_config = cache.get(cache_key)
            if cached_config:
                return cached_config

        config = ConfigPaiement.objects.filter(pk=config_paiement_id).first()
        if not config:
            logger.error(
                "[PayGate] Configuration de paiement %s introuvable.",
                config_paiement_id,
            )
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=400,
                dev_message=(
                    f"La configuration de paiement {config_paiement_id} "
                    "est introuvable."
                ),
            )

        config_data = {
            "id":          config.id,
            "compte_id":  config.compte_id,
            "api_key":     config.api_key,
            "base_url":    config.base_url.rstrip('/'),
            "success_url": config.success_url,
        }
        cache.set(cache_key, config_data, 86400)
        return config_data

    # ------------------------------------------------------------------ #
    # INITIATION : checkout puis collect
    # ------------------------------------------------------------------ #
    @classmethod
    def traiter_paiement_complet(
        cls,
        config_paiement_id,
        montant,
        external_reference,
        phone_number,
    ):
        """
        Enchaîne checkout puis collect dans une session HTTP unique (Keep-Alive).

        Retour possible :
          {
              "paygate_reference": str,   # toujours présent si le checkout a réussi
              "session_token":     str,
              "collect_ok":        bool,
              "collect_error":     str | None,  # message brut PayGate si collect KO
          }

        Lève CustomAPIException si le CHECKOUT échoue
        (rien n'a été créé côté PayGate → ECHEC certain côté appelant).
        """
        config = cls._get_config(config_paiement_id)
        base_url    = config['base_url']
        local_phone = format_phone_cm(phone_number)

        with requests.Session() as http:

            # ============================================================
            # ÉTAPE 1 : CHECKOUT
            # Échec → lève une exception (rien n'existe).
            # ============================================================
            url_checkout = f"{base_url}/create-checkout-session/"
            checkout_payload = {
                "amount":             int(montant),
                "phone_number":       local_phone,
                "external_reference": external_reference,
                "currency":           "XAF",
                "success_url":        config['success_url'],
                "cancel_url":         "",
            }

            try:
                headers = cls._auth_headers(config['api_key'])
                response_checkout = http.post(
                    url_checkout, json=checkout_payload, headers=headers, timeout=15
                )

                # Clé expirée : on rafraîchit le cache et on retente une fois.
                if response_checkout.status_code == 401:
                    logger.warning("[PayGate] Clé d'API rejetée au checkout. Rafraîchissement...")
                    config = cls._get_config(
                        config_paiement_id,
                        force_refresh=True,
                    )
                    headers = cls._auth_headers(config['api_key'])
                    response_checkout = http.post(
                        url_checkout, json=checkout_payload, headers=headers, timeout=15
                    )

                response_checkout.raise_for_status()

            except requests.exceptions.RequestException as e:
                code_http = 504
                details   = "Le serveur n'a pas répondu (Timeout/Coupure réseau)."
                if e.response is not None:
                    code_http = e.response.status_code
                    details   = e.response.text
                    logger.error(f"[PayGate] Détail erreur checkout : {details}")
                logger.exception(f"[PayGate] Échec du checkout pour la ref {external_reference}")
                raise CustomAPIException(
                    resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                    status_code=code_http,
                    dev_message=f"Checkout ({code_http}) : {details}"
                )

            try:
                checkout_data = response_checkout.json()
            except ValueError:
                raise CustomAPIException(
                    resp_code=ErrorCodes.SYSTEM_ERROR,
                    status_code=502,
                    dev_message="PayGate a répondu au checkout avec un format invalide (Non-JSON)."
                )

            session_token     = checkout_data.get('session_token')
            paygate_reference = checkout_data.get('reference')

            if not paygate_reference:
                raise CustomAPIException(
                    resp_code=ErrorCodes.SYSTEM_ERROR,
                    status_code=502,
                    dev_message="PayGate a validé le checkout sans renvoyer de référence."
                )

            # ============================================================
            # ÉTAPE 2 : COLLECT
            # La session PayGate existe. On tente le push USSD.
            # On ne lève JAMAIS d'exception ici : on remonte collect_ok + collect_error.
            # ============================================================
            url_collect = f"{base_url}/collect/"
            collect_payload = {
                "phone_number":  local_phone,
                "session_token": session_token,
            }
            public_headers = {"Content-Type": "application/json"}

            collect_ok    = False
            collect_error = None

            try:
                logger.info(
                    f"[PayGate] Envoi du push USSD (collect) pour le token {session_token}"
                )
                response_collect = http.post(
                    url_collect, json=collect_payload,
                    headers=public_headers, timeout=15
                )
                response_collect.raise_for_status()
                response_collect.json()   # on valide que le corps est lisible
                collect_ok = True

            except requests.exceptions.RequestException as e:
                # PayGate a renvoyé une réponse structurée (4xx/5xx) ou coupure réseau.
                if e.response is not None:
                    try:
                        body = e.response.json()
                        # On cherche le message le plus lisible dans la réponse PayGate.
                        collect_error = (
                            body.get('message')
                            or body.get('detail')
                            or body.get('error')
                            or str(body)
                        )
                    except ValueError:
                        collect_error = e.response.text or f"HTTP {e.response.status_code}"
                else:
                    collect_error = "La passerelle n'a pas répondu (timeout ou coupure réseau)."

                logger.warning(
                    f"[PayGate] Collect KO pour {paygate_reference}. "
                    f"Erreur : {collect_error}"
                )

            except ValueError:
                collect_error = "Réponse illisible reçue de la passerelle après le collect."
                logger.warning(
                    f"[PayGate] Collect réponse non-JSON pour {paygate_reference}."
                )

            return {
                "paygate_reference": paygate_reference,
                "session_token":     session_token,
                "collect_ok":        collect_ok,
                "collect_error":     collect_error,
            }

    # ------------------------------------------------------------------ #
    # VÉRIFICATION (pull) — source de vérité pour le polling
    # ------------------------------------------------------------------ #
    @classmethod
    def verifier_statut_transaction(
        cls,
        config_paiement_id,
        gateway_reference,
    ):
        """
        Interroge PayGate sur l'état réel d'une transaction.
        Filet de sécurité si le webhook est perdu ou en retard.
        """
        if not gateway_reference:
            # ValueError intentionnelle : le polling doit la distinguer
            # d'une vraie panne réseau pour ne pas boucler inutilement.
            raise ValueError("Une gateway_reference est requise pour vérifier la transaction.")

        config = cls._get_config(config_paiement_id)
        url    = f"{config['base_url']}/transaction/{gateway_reference}/"

        try:
            logger.info(
                f"[PayGate] Vérification de la transaction "
                f"{gateway_reference} (Configuration {config_paiement_id})"
            )
            headers  = cls._auth_headers(config['api_key'])
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 401:
                logger.warning("[PayGate] Clé rejetée à la vérification. Rafraîchissement...")
                config = cls._get_config(
                    config_paiement_id,
                    force_refresh=True,
                )
                headers  = cls._auth_headers(config['api_key'])
                response = requests.get(url, headers=headers, timeout=10)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            code_http = 504
            details   = "Impossible de joindre la passerelle pour vérification."
            if e.response is not None:
                code_http = e.response.status_code
                details   = e.response.text
                logger.error(
                    f"[PayGate] Détail échec vérification "
                    f"(Ref: {gateway_reference}) : {details}"
                )
            logger.exception(f"[PayGate] Erreur API Verify [{url}]")
            raise CustomAPIException(
                resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                status_code=code_http,
                dev_message=f"Vérification ({code_http}) : {details}"
            )

    # ------------------------------------------------------------------ #
    # Helper
    # ------------------------------------------------------------------ #
    @staticmethod
    def _auth_headers(api_key):
        return {
            "Authorization": f"Api-Key {api_key}",
            "Content-Type":  "application/json",
        }
