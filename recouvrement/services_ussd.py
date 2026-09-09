import json
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django_q.tasks import async_task

from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from core.utils import format_phone_cm


class PaiementUSSDService:
    """Orchestre l'initiation et la soumission d'une preuve USSD."""

    STATUTS_SESSION_BLOQUANTS = (
        'EN_ATTENTE',
        'EN_VERIFICATION',
    )

    @staticmethod
    def _erreur(
        message,
        code=ErrorCodes.BAD_REQUEST,
        status_code=400,
    ):
        raise CustomAPIException(
            resp_code=code,
            status_code=status_code,
            dev_message=message,
        )

    @staticmethod
    def _nom_contrainte(exception):
        cause = getattr(exception, '__cause__', None)
        diagnostic = getattr(cause, 'diag', None)
        return getattr(diagnostic, 'constraint_name', None)

    @staticmethod
    def _planifier_ventilation(session_id):
        transaction.on_commit(
            lambda: async_task(
                'core.tasks.paiement_task',
                session_id,
                'SUCCESS',
            ),
            robust=True,
        )

    @classmethod
    def initier_session(cls, *, utilisateur, lignes, operateur, telephone=''):
        from .models import (
            CompteReceptionProprietaire,
            Lease,
            Paiement,
            SessionPaiement,
        )

        montants_par_lease = {
            ligne['lease_id'].id: ligne['montant']
            for ligne in lignes
        }
        lease_ids = list(montants_par_lease)
        operateur = (operateur or '').strip().upper()

        try:
            with transaction.atomic():
                leases = list(
                    Lease.objects
                    .select_for_update(of=('self',))
                    .select_related(
                        'contrat',
                        'contrat__proprietaire',
                        'contrat__agence',
                    )
                    .filter(pk__in=lease_ids)
                    .order_by('id')
                )

                if len(leases) != len(lease_ids):
                    cls._erreur(
                        "Une ou plusieurs échéances sont introuvables."
                    )

                comptes = {lease.contrat.compte_id for lease in leases}
                if len(comptes) != 1:
                    cls._erreur(
                        "Les échéances sélectionnées appartiennent à des "
                        "comptes différents."
                    )
                compte_id = next(iter(comptes))

                if (
                    not utilisateur.is_superuser
                    and compte_id != utilisateur.compte_id
                ):
                    cls._erreur(
                        "Les échéances sélectionnées ne sont pas accessibles.",
                        ErrorCodes.FORBIDDEN,
                        403,
                    )

                if not utilisateur.is_staff and not utilisateur.is_superuser:
                    if any(
                        lease.contrat.chauffeur_id != utilisateur.id
                        for lease in leases
                    ):
                        cls._erreur(
                            "Les échéances sélectionnées ne sont pas "
                            "accessibles.",
                            ErrorCodes.FORBIDDEN,
                            403,
                        )

                contrats_sans_agence = [
                    lease.contrat.reference
                    for lease in leases
                    if lease.contrat.agence_id is None
                ]
                if contrats_sans_agence:
                    cls._erreur(
                        "Paiement USSD impossible : chaque contrat doit être "
                        "rattaché à une agence. Contrat(s) concerné(s) : "
                        f"{', '.join(sorted(set(contrats_sans_agence)))}."
                    )

                agences = {lease.contrat.agence_id for lease in leases}
                if len(agences) != 1:
                    cls._erreur(
                        "Paiement groupé impossible : veuillez effectuer "
                        "des paiements séparés par agence."
                    )
                agence_id = next(iter(agences))

                contrats_sans_proprietaire = [
                    lease.contrat.reference
                    for lease in leases
                    if lease.contrat.proprietaire_id is None
                ]
                if contrats_sans_proprietaire:
                    cls._erreur(
                        "Paiement USSD impossible : chaque contrat doit être "
                        "rattaché à un propriétaire. Contrat(s) concerné(s) : "
                        f"{', '.join(sorted(set(contrats_sans_proprietaire)))}."
                    )

                proprietaire_ids = {
                    lease.contrat.proprietaire_id for lease in leases
                }
                if len(proprietaire_ids) != 1:
                    cls._erreur(
                        "Les échéances sélectionnées appartiennent à des "
                        "propriétaires différents. Veuillez effectuer des "
                        "paiements séparés."
                    )

                proprietaire = leases[0].contrat.proprietaire
                if proprietaire.compte_id != compte_id:
                    cls._erreur(
                        "Le propriétaire et les contrats appartiennent à des "
                        "comptes différents."
                    )
                if not proprietaire.actif:
                    cls._erreur(
                        "Le propriétaire associé à ces contrats est inactif."
                    )

                compte_reception = (
                    CompteReceptionProprietaire.objects
                    .select_for_update()
                    .filter(
                        compte_id=compte_id,
                        proprietaire=proprietaire,
                        operateur=operateur,
                        actif=True,
                    )
                    .first()
                )
                if compte_reception is None:
                    cls._erreur(
                        f"Aucun compte {operateur} actif n'est configuré pour "
                        f"le propriétaire {proprietaire.nom_complet}."
                    )

                paiements_bloquants = set(
                    Paiement.objects.filter(
                        lease_id__in=lease_ids,
                        methode=Paiement.METHODE_USSD_ASSISTE,
                        statut=Paiement.STATUT_EN_ATTENTE,
                        est_annule=False,
                        session__canal=SessionPaiement.CANAL_USSD_ASSISTE,
                        session__statut__in=cls.STATUTS_SESSION_BLOQUANTS,
                    ).values_list('lease_id', flat=True)
                )
                if paiements_bloquants:
                    ids = ', '.join(
                        str(lease_id)
                        for lease_id in sorted(paiements_bloquants)
                    )
                    cls._erreur(
                        "Un paiement USSD est déjà en cours ou en "
                        f"vérification pour les échéances : {ids}."
                    )

                for lease in leases:
                    if lease.statut in (
                        Lease.STATUT_PAYE,
                        Lease.STATUT_ANNULE,
                    ):
                        cls._erreur(
                            f"L'échéance {lease.id} ne peut plus être payée."
                        )

                    montant = montants_par_lease[lease.id]
                    reste = lease.montant_attendu - lease.montant_paye
                    if montant <= 0 or montant > reste:
                        cls._erreur(
                            f"Le montant demandé pour l'échéance {lease.id} "
                            "est invalide."
                        )

                montant_total = sum(
                    montants_par_lease.values(),
                    Decimal('0.00'),
                )
                session = SessionPaiement.objects.create(
                    compte_id=compte_id,
                    agence_id=agence_id,
                    reference=SessionPaiement.generer_reference_session(
                        prefixe='USSD'
                    ),
                    canal=SessionPaiement.CANAL_USSD_ASSISTE,
                    montant_total=montant_total,
                    telephone=format_phone_cm(telephone),
                    utilisateur=utilisateur,
                    config_paiement=None,
                    proprietaire=proprietaire,
                    compte_reception=compte_reception,
                    operateur=operateur,
                    numero_destinataire=compte_reception.numero,
                    nom_destinataire=compte_reception.nom_titulaire,
                    statut=SessionPaiement.STATUT_EN_ATTENTE,
                )

                for lease in leases:
                    Paiement.objects.create(
                        session=session,
                        contrat=lease.contrat,
                        lease=lease,
                        enregistre_par=utilisateur,
                        compte_id=compte_id,
                        agence_id=agence_id,
                        montant=montants_par_lease[lease.id],
                        methode=Paiement.METHODE_USSD_ASSISTE,
                        statut=Paiement.STATUT_EN_ATTENTE,
                    )

                return session
        except IntegrityError as exc:
            if cls._nom_contrainte(exc) == 'uniq_paie_ussd_attente_lease':
                cls._erreur(
                    "Un paiement USSD est déjà en cours pour au moins une "
                    "des échéances sélectionnées."
                )
            raise

    @classmethod
    def soumettre_preuve(cls, *, session, donnees, payload_capture):
        from .models import PreuvePaiementUSSD, SessionPaiement

        try:
            with transaction.atomic():
                session = (
                    SessionPaiement.objects
                    .select_for_update(of=('self',))
                    .select_related('compte_reception', 'proprietaire')
                    .get(pk=session.pk)
                )

                if session.canal != SessionPaiement.CANAL_USSD_ASSISTE:
                    cls._erreur(
                        "Cette session n'est pas un paiement USSD assisté."
                    )
                if session.statut != SessionPaiement.STATUT_EN_ATTENTE:
                    cls._erreur(
                        "Cette session n'accepte plus de nouvelle preuve."
                    )
                if PreuvePaiementUSSD.objects.filter(
                    session=session,
                ).exists():
                    cls._erreur(
                        "Une preuve a déjà été soumise pour cette session."
                    )

                operateur = donnees['network']
                reference_operateur = donnees['reference'].strip().upper()
                numero_expediteur = format_phone_cm(donnees['senderPhone'])
                numero_destinataire = format_phone_cm(
                    donnees['recipientPhone']
                )

                if operateur != session.operateur:
                    cls._erreur(
                        "L'opérateur de la preuve ne correspond pas à celui "
                        "de la session."
                    )
                if numero_destinataire != session.numero_destinataire:
                    cls._erreur(
                        "Le numéro destinataire ne correspond pas au compte "
                        "de réception du propriétaire."
                    )
                if donnees['amount'] != session.montant_total:
                    cls._erreur(
                        "Le montant transféré ne correspond pas au montant "
                        "total de la session."
                    )
                if (
                    session.telephone
                    and numero_expediteur != session.telephone
                ):
                    cls._erreur(
                        "Le numéro expéditeur ne correspond pas au numéro "
                        "déclaré lors de l'initiation."
                    )

                payload_json = json.loads(json.dumps(
                    payload_capture,
                    default=str,
                ))
                preuve = PreuvePaiementUSSD.objects.create(
                    compte_id=session.compte_id,
                    session=session,
                    operateur=operateur,
                    reference_operateur=reference_operateur,
                    texte_brut=donnees['rawText'],
                    montant_transfere=donnees['amount'],
                    frais_operateur=donnees.get('fee', Decimal('0.00')),
                    commission=donnees.get('commission', Decimal('0.00')),
                    nouveau_solde=donnees.get('newBalance'),
                    nom_expediteur=donnees.get('senderName', ''),
                    numero_expediteur=numero_expediteur,
                    nom_destinataire=donnees.get('recipientName', ''),
                    numero_destinataire=numero_destinataire,
                    date_transaction=donnees.get('timestamp'),
                    capture_appareil_le=donnees['capturedAtDevice'],
                    payload_capture=payload_json,
                    statut=PreuvePaiementUSSD.STATUT_EN_VERIFICATION,
                )

                session.statut = SessionPaiement.STATUT_EN_VERIFICATION
                session.gateway_reference = reference_operateur
                session.telephone = numero_expediteur
                session.save(update_fields=[
                    'statut',
                    'gateway_reference',
                    'telephone',
                    'updated_at',
                ])

                return preuve
        except IntegrityError as exc:
            if cls._nom_contrainte(exc) in {
                'uniq_preuve_ref_oper_compte',
                'rc_preuve_paiement_ussd_session_id_key',
            }:
                cls._erreur(
                    "Cette preuve ou cette référence de transaction a déjà "
                    "été utilisée."
                )
            raise

    @classmethod
    def valider_preuve(cls, *, preuve_id, agent):
        """Valide la preuve puis délègue la ventilation à paiement_task."""
        from .models import Paiement, PreuvePaiementUSSD, SessionPaiement

        with transaction.atomic():
            preuve = (
                PreuvePaiementUSSD.objects
                .select_for_update(of=('self',))
                .select_related('session')
                .get(pk=preuve_id)
            )
            session = (
                SessionPaiement.objects
                .select_for_update()
                .get(pk=preuve.session_id)
            )

            if not agent.is_superuser and preuve.compte_id != agent.compte_id:
                cls._erreur(
                    "Cette preuve appartient à un autre compte partenaire.",
                    ErrorCodes.FORBIDDEN,
                    403,
                )
            if preuve.statut == PreuvePaiementUSSD.STATUT_VALIDEE:
                if Paiement.objects.filter(
                    session=session,
                    compte_id=session.compte_id,
                    methode=Paiement.METHODE_USSD_ASSISTE,
                    statut=Paiement.STATUT_EN_ATTENTE,
                ).exists():
                    cls._planifier_ventilation(session.id)
                return preuve, False
            if preuve.statut != PreuvePaiementUSSD.STATUT_EN_VERIFICATION:
                cls._erreur("Seule une preuve en vérification peut être validée.")
            if session.statut != SessionPaiement.STATUT_EN_VERIFICATION:
                cls._erreur(
                    "La session associée n'est plus en vérification."
                )

            lignes = list(
                Paiement.objects
                .select_for_update(of=('self',))
                .select_related('lease')
                .filter(
                    session=session,
                    compte_id=session.compte_id,
                    methode=Paiement.METHODE_USSD_ASSISTE,
                    statut=Paiement.STATUT_EN_ATTENTE,
                    est_annule=False,
                )
                .order_by('id')
            )
            if not lignes:
                cls._erreur(
                    "La session ne contient aucune ligne USSD à valider."
                )
            if sum(
                (ligne.montant for ligne in lignes),
                Decimal('0.00'),
            ) != session.montant_total:
                cls._erreur(
                    "Le total des lignes ne correspond plus au montant de "
                    "la session."
                )
            if preuve.montant_transfere != session.montant_total:
                cls._erreur(
                    "Le montant de la preuve ne correspond plus à la session."
                )

            for ligne in lignes:
                if ligne.lease_id is None:
                    cls._erreur(
                        f"La ligne de paiement {ligne.id} n'a aucun lease."
                    )
                reste = (
                    ligne.lease.montant_attendu
                    - ligne.lease.montant_paye
                )
                if ligne.montant > reste:
                    cls._erreur(
                        f"Le lease {ligne.lease_id} a été modifié depuis la "
                        "soumission de la preuve."
                    )

            maintenant = timezone.now()
            preuve.statut = PreuvePaiementUSSD.STATUT_VALIDEE
            preuve.verifie_par = agent
            preuve.verifie_le = maintenant
            preuve.motif_rejet = ''
            preuve.save(update_fields=[
                'statut',
                'verifie_par',
                'verifie_le',
                'motif_rejet',
                'updated_at',
            ])

            session.statut = SessionPaiement.STATUT_VALIDE
            session.date_validation = maintenant
            session.save(update_fields=[
                'statut',
                'date_validation',
                'updated_at',
            ])

            cls._planifier_ventilation(session.id)
            return preuve, True

    @classmethod
    def rejeter_preuve(cls, *, preuve_id, agent, motif):
        """Rejette la preuve et libère ses leases pour un autre paiement."""
        from .models import Paiement, PreuvePaiementUSSD, SessionPaiement

        motif = (motif or '').strip()
        if not motif:
            cls._erreur("Le motif du rejet est obligatoire.")

        with transaction.atomic():
            preuve = (
                PreuvePaiementUSSD.objects
                .select_for_update(of=('self',))
                .select_related('session')
                .get(pk=preuve_id)
            )
            session = (
                SessionPaiement.objects
                .select_for_update()
                .get(pk=preuve.session_id)
            )

            if not agent.is_superuser and preuve.compte_id != agent.compte_id:
                cls._erreur(
                    "Cette preuve appartient à un autre compte partenaire.",
                    ErrorCodes.FORBIDDEN,
                    403,
                )
            if preuve.statut == PreuvePaiementUSSD.STATUT_REJETEE:
                return preuve, False
            if preuve.statut != PreuvePaiementUSSD.STATUT_EN_VERIFICATION:
                cls._erreur("Seule une preuve en vérification peut être rejetée.")
            if session.statut != SessionPaiement.STATUT_EN_VERIFICATION:
                cls._erreur(
                    "La session associée n'est plus en vérification."
                )

            maintenant = timezone.now()
            preuve.statut = PreuvePaiementUSSD.STATUT_REJETEE
            preuve.verifie_par = agent
            preuve.verifie_le = maintenant
            preuve.motif_rejet = motif
            preuve.save(update_fields=[
                'statut',
                'verifie_par',
                'verifie_le',
                'motif_rejet',
                'updated_at',
            ])

            session.statut = SessionPaiement.STATUT_REJETE
            session.save(update_fields=['statut', 'updated_at'])
            Paiement.objects.filter(
                session=session,
                compte_id=session.compte_id,
                methode=Paiement.METHODE_USSD_ASSISTE,
                statut=Paiement.STATUT_EN_ATTENTE,
            ).update(
                statut=Paiement.STATUT_ECHEC,
                updated_at=maintenant,
            )
            return preuve, True
