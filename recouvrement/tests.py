import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.forms.models import model_to_dict
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from django_q.models import Schedule, Task
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import (
    ConfigPaiement,
    CustomUser,
    CustomUserRole,
    Role,
)
from core.exceptions import CustomAPIException
from core.tasks import (
    generer_leases_task,
    paiement_task,
    verifier_statut_session_task,
)
from recouvrement.admin import (
    Transaction,
    AssignerConfigPaiementContratsForm,
    AssignerRegleGenerationContratsForm,
    ContratAdminForm,
    RegleGenerationLeaseAdminForm,
)
from recouvrement.models import (
    Agence,
    CompteReceptionProprietaire,
    Contrat,
    Lease,
    Parametre,
    Paiement,
    Penalite,
    PreuvePaiementUSSD,
    Proprietaire,
    RegleGenerationLease,
    SessionPaiement,
    TypeContrat,
)
from recouvrement.api.v1.views import (
    AgenceViewSet,
    ContratViewSet,
    InitiationPaiementView,
    InitiationPaiementUSSDView,
    LeaseViewSet,
    PaiementViewSet,
    PenaliteViewSet,
    RegleGenerationLeaseViewSet,
    SessionPaiementViewSet,
    SoumissionPreuvePaiementUSSDView,
    WebhookView,
)
from recouvrement.api.v1.serializers import (
    AgenceSerializer,
    CalendrierSerializer,
    ContratSerializer,
    LeaseSerializer,
    PaiementSerializer,
    PenaliteSerializer,
    SessionPaiementSerializer,
    SousContratSerializer,
)
from recouvrement.services import (
    PaymentService,
    assurer_lease_suivant_du_lease,
    calculer_prochaine_occurrence,
    generer_leases_pour_regle,
)
from recouvrement.services_ussd import PaiementUSSDService


def occurrence_aware(annee, mois, jour, heure, minute=0):
    return timezone.make_aware(
        datetime(annee, mois, jour, heure, minute),
        timezone.get_current_timezone(),
    )


class AgenceZoneTests(TestCase):
    def test_enregistrement_normalise_zone_code_et_zone_search(self):
        agence = Agence.objects.create(
            compte_id=91,
            nom='Agence Akwa',
            zone='  Littoral Édéa  ',
            code=' douala-akwa ',
        )

        self.assertEqual(agence.zone, 'LITTORAL ÉDÉA')
        self.assertEqual(agence.zone_search, 'littoral edea')
        self.assertEqual(agence.code, 'DOUALA-AKWA')

    def test_update_fields_zone_met_aussi_zone_search_a_jour(self):
        agence = Agence.objects.create(
            compte_id=91,
            nom='Agence Akwa',
            zone='Douala',
            code='DLA-AKWA',
        )

        agence.zone = '  Yaoundé  '
        agence.save(update_fields=['zone'])
        agence.refresh_from_db()

        self.assertEqual(agence.zone, 'YAOUNDÉ')
        self.assertEqual(agence.zone_search, 'yaounde')

    def test_serializer_expose_la_zone_normalisee(self):
        agence = Agence.objects.create(
            compte_id=91,
            nom='Agence Deido',
            zone='Douala',
            code='DLA-DEIDO',
        )

        self.assertEqual(
            AgenceSerializer(agence).data['zone'],
            'DOUALA',
        )

    def test_index_zone_search_et_index_code_sont_declares(self):
        index_par_nom = {
            index.name: index
            for index in Agence._meta.indexes
        }

        self.assertEqual(
            index_par_nom['idx_agence_zone_search_trgm'].fields,
            ['zone_search'],
        )
        self.assertEqual(
            index_par_nom['idx_agence_zone_search_trgm'].opclasses,
            ['gin_trgm_ops'],
        )
        self.assertEqual(
            index_par_nom['idx_agence_code'].fields,
            ['code'],
        )


class RechercheAgenceTests(SimpleTestCase):
    def test_recherche_api_agence_inclut_zone_et_code(self):
        self.assertIn('zone_search', AgenceViewSet.search_fields)
        self.assertIn('code', AgenceViewSet.search_fields)

    def test_recherche_api_des_modeles_agence_inclut_zone_et_code(self):
        viewsets = [
            ContratViewSet,
            LeaseViewSet,
            PaiementViewSet,
            PenaliteViewSet,
            SessionPaiementViewSet,
        ]

        for viewset in viewsets:
            with self.subTest(viewset=viewset.__name__):
                self.assertIn(
                    'agence__zone_search',
                    viewset.search_fields,
                )
                self.assertIn('agence__code', viewset.search_fields)

    def test_recherche_admin_des_modeles_agence_inclut_zone_et_code(self):
        agence_admin = admin.site._registry[Agence]
        self.assertIn('zone_search', agence_admin.search_fields)
        self.assertIn('code', agence_admin.search_fields)

        for model in [Contrat, Lease, Paiement, Penalite, Transaction]:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model._meta.label):
                self.assertIn(
                    'agence__zone_search',
                    model_admin.search_fields,
                )
                self.assertIn('agence__code', model_admin.search_fields)


class ProprietaireContratTests(TestCase):
    compte_id = 91

    def setUp(self):
        self.chauffeur = CustomUser.objects.create(
            keycloak_id='filgrace-owner-driver',
            compte_id=self.compte_id,
            nom_complet='Chauffeur Filgrace',
            is_active=True,
        )
        self.type_parent = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Moto Filgrace',
            code='MOTO-FILGRACE',
            est_principal=True,
        )
        self.type_enfant = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Royal Care Filgrace',
            code='CARE-FILGRACE',
            est_principal=False,
        )
        self.proprietaire = Proprietaire.objects.create(
            compte_id=self.compte_id,
            nom_complet='  Étienne Propriétaire  ',
        )
        self.autre_proprietaire = Proprietaire.objects.create(
            compte_id=self.compte_id,
            nom_complet='Autre propriétaire',
        )
        self.proprietaire_autre_compte = Proprietaire.objects.create(
            compte_id=92,
            nom_complet='Propriétaire autre compte',
        )

    def _creer_contrat(
        self,
        *,
        parent=None,
        proprietaire=None,
        type_contrat=None,
    ):
        return Contrat.objects.create(
            compte_id=self.compte_id,
            chauffeur=self.chauffeur,
            type_contrat=type_contrat or (
                self.type_enfant if parent else self.type_parent
            ),
            parent=parent,
            proprietaire=proprietaire or self.proprietaire,
            nom_complet='Contrat Filgrace',
            montant_total=Decimal('100000.00'),
            montant_restant=Decimal('100000.00'),
            montant_par_paiement=Decimal('3500.00'),
            montant_paye=Decimal('0.00'),
            frequence=Contrat.JOURNALIER,
            date_debut=date(2026, 9, 1),
            date_fin=date(2026, 12, 31),
            prochaine_echeance=occurrence_aware(2026, 9, 1, 8),
            statut=Contrat.STATUT_ACTIF,
        )

    def test_proprietaire_et_compte_reception_sont_normalises(self):
        compte_reception = CompteReceptionProprietaire.objects.create(
            compte_id=self.compte_id,
            proprietaire=self.proprietaire,
            operateur=CompteReceptionProprietaire.OPERATEUR_ORANGE,
            numero=' 690-16-96-94 ',
        )

        self.proprietaire.refresh_from_db()
        self.assertEqual(
            self.proprietaire.nom_complet,
            'Étienne Propriétaire',
        )
        self.assertEqual(
            self.proprietaire.nom_complet_search,
            'etienne proprietaire',
        )
        self.assertEqual(compte_reception.numero, '237690169694')
        self.assertEqual(
            compte_reception.nom_titulaire,
            'Étienne Propriétaire',
        )
        self.assertEqual(
            compte_reception.nom_titulaire_search,
            'etienne proprietaire',
        )

    def test_un_seul_compte_par_operateur_et_par_proprietaire(self):
        CompteReceptionProprietaire.objects.create(
            compte_id=self.compte_id,
            proprietaire=self.proprietaire,
            operateur=CompteReceptionProprietaire.OPERATEUR_MTN,
            numero='677777770',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompteReceptionProprietaire.objects.create(
                    compte_id=self.compte_id,
                    proprietaire=self.proprietaire,
                    operateur=CompteReceptionProprietaire.OPERATEUR_MTN,
                    numero='677777771',
                )

    def test_compte_reception_refuse_un_proprietaire_d_un_autre_compte(self):
        with self.assertRaises(ValidationError):
            CompteReceptionProprietaire.objects.create(
                compte_id=self.compte_id,
                proprietaire=self.proprietaire_autre_compte,
                operateur=CompteReceptionProprietaire.OPERATEUR_ORANGE,
                numero='690169694',
            )

    def test_compte_partenaire_du_proprietaire_est_immuable(self):
        self.proprietaire.compte_id = 92

        with self.assertRaises(ValidationError):
            self.proprietaire.save(update_fields=['compte_id'])

        self.proprietaire.refresh_from_db()
        self.assertEqual(self.proprietaire.compte_id, self.compte_id)

    def test_sous_contrat_herite_toujours_du_proprietaire(self):
        parent = self._creer_contrat()
        enfant = self._creer_contrat(
            parent=parent,
            proprietaire=self.autre_proprietaire,
        )

        self.assertEqual(enfant.proprietaire_id, self.proprietaire.id)

        enfant.proprietaire = self.autre_proprietaire
        enfant.save(update_fields=['proprietaire'])
        enfant.refresh_from_db()
        self.assertEqual(enfant.proprietaire_id, self.proprietaire.id)

    def test_changement_parent_propage_le_proprietaire_sans_historique(self):
        parent = self._creer_contrat()
        enfant = self._creer_contrat(parent=parent)

        parent.proprietaire = self.autre_proprietaire
        parent.save(update_fields=['proprietaire'])
        enfant.refresh_from_db()

        self.assertEqual(
            enfant.proprietaire_id,
            self.autre_proprietaire.id,
        )

    def test_changement_proprietaire_bloque_avec_historique_enfant(self):
        parent = self._creer_contrat()
        enfant = self._creer_contrat(parent=parent)
        Lease.objects.create(
            compte_id=self.compte_id,
            contrat=enfant,
            date_echeance=occurrence_aware(2026, 9, 1, 8),
            montant_attendu=Decimal('3500.00'),
        )

        parent.proprietaire = self.autre_proprietaire
        with self.assertRaises(ValidationError):
            parent.save(update_fields=['proprietaire'])

        parent.refresh_from_db()
        self.assertEqual(parent.proprietaire_id, self.proprietaire.id)

    def test_contrat_refuse_proprietaire_d_un_autre_compte(self):
        with self.assertRaises(ValidationError):
            self._creer_contrat(
                proprietaire=self.proprietaire_autre_compte,
            )

    def test_serializer_exige_proprietaire_sur_nouveau_parent(self):
        request = SimpleNamespace(
            user=self.chauffeur,
            data={},
        )
        serializer = ContratSerializer(
            data={
                'chauffeur': self.chauffeur.id,
                'type_contrat': self.type_parent.id,
                'immatriculation': 'LT-001-FG',
                'vin': 'FILGRACE000000001',
                'montant_total': '100000.00',
                'montant_par_paiement': '3500.00',
                'frequence': Contrat.JOURNALIER,
                'date_debut': '2026-09-01',
                'date_fin': '2026-12-31',
                'prochaine_echeance': '2026-09-01T08:00:00+01:00',
            },
            context={'request': request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('proprietaire', serializer.errors)

    def test_admin_interdit_modification_proprietaire_sous_contrat(self):
        parent = self._creer_contrat()
        enfant = self._creer_contrat(parent=parent)
        contrat_admin = admin.site._registry[Contrat]

        champs = contrat_admin.get_readonly_fields(
            SimpleNamespace(user=self.chauffeur),
            enfant,
        )

        self.assertIn('proprietaire', champs)


class PaiementUSSDAssisteTests(TestCase):
    compte_id = 93

    def setUp(self):
        self.utilisateur = CustomUser.objects.create(
            keycloak_id='filgrace-ussd-user',
            compte_id=self.compte_id,
            nom_complet='Chauffeur paiement USSD',
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.deuxieme_chauffeur = CustomUser.objects.create(
            keycloak_id='filgrace-ussd-driver-2',
            compte_id=self.compte_id,
            nom_complet='Deuxième chauffeur USSD',
            is_active=True,
        )
        self.troisieme_chauffeur = CustomUser.objects.create(
            keycloak_id='filgrace-ussd-driver-3',
            compte_id=self.compte_id,
            nom_complet='Troisième chauffeur USSD',
            is_active=True,
        )
        self.type_contrat = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Moto paiement USSD',
            code='MOTO-USSD',
            est_principal=True,
        )
        self.agence = Agence.objects.create(
            compte_id=self.compte_id,
            nom='Agence Filgrace',
            code='FILGRACE',
        )
        self.proprietaire = Proprietaire.objects.create(
            compte_id=self.compte_id,
            nom_complet='Propriétaire principal',
        )
        self.autre_proprietaire = Proprietaire.objects.create(
            compte_id=self.compte_id,
            nom_complet='Autre propriétaire',
        )
        self.compte_orange = CompteReceptionProprietaire.objects.create(
            compte_id=self.compte_id,
            proprietaire=self.proprietaire,
            operateur=CompteReceptionProprietaire.OPERATEUR_ORANGE,
            numero='690169694',
            nom_titulaire='Propriétaire principal',
        )
        self.autre_compte_orange = (
            CompteReceptionProprietaire.objects.create(
                compte_id=self.compte_id,
                proprietaire=self.autre_proprietaire,
                operateur=CompteReceptionProprietaire.OPERATEUR_ORANGE,
                numero='690169695',
                nom_titulaire='Autre propriétaire',
            )
        )
        self.contrat = self._creer_contrat(
            'Contrat USSD principal',
            self.proprietaire,
        )
        self.contrat_meme_proprietaire = self._creer_contrat(
            'Deuxième contrat du propriétaire',
            self.proprietaire,
            chauffeur=self.deuxieme_chauffeur,
        )
        self.contrat_autre_proprietaire = self._creer_contrat(
            'Contrat autre propriétaire',
            self.autre_proprietaire,
            chauffeur=self.troisieme_chauffeur,
        )
        self.lease = self._creer_lease(
            self.contrat,
            Decimal('3500.00'),
        )
        self.lease_meme_proprietaire = self._creer_lease(
            self.contrat_meme_proprietaire,
            Decimal('1000.00'),
        )
        self.lease_autre_proprietaire = self._creer_lease(
            self.contrat_autre_proprietaire,
            Decimal('2500.00'),
        )

    def _creer_contrat(self, nom, proprietaire, chauffeur=None):
        return Contrat.objects.create(
            compte_id=self.compte_id,
            agence=self.agence,
            chauffeur=chauffeur or self.utilisateur,
            enregistre_par=self.utilisateur,
            type_contrat=self.type_contrat,
            proprietaire=proprietaire,
            nom_complet=nom,
            montant_total=Decimal('200000.00'),
            montant_restant=Decimal('200000.00'),
            montant_par_paiement=Decimal('3500.00'),
            montant_paye=Decimal('0.00'),
            frequence=Contrat.JOURNALIER,
            date_debut=date(2026, 9, 1),
            date_fin=date(2027, 3, 31),
            prochaine_echeance=occurrence_aware(2026, 9, 2, 8),
            statut=Contrat.STATUT_ACTIF,
        )

    def _creer_lease(self, contrat, montant):
        return Lease.objects.create(
            compte_id=self.compte_id,
            contrat=contrat,
            date_echeance=occurrence_aware(2026, 9, 1, 8),
            montant_attendu=montant,
        )

    def _initier(self, lignes, operateur='orange'):
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement-ussd/',
            {
                'lignes': [
                    {
                        'lease_id': lease.id,
                        'montant': str(montant),
                    }
                    for lease, montant in lignes
                ],
                'operateur': operateur,
                'phone_number': '690458393',
            },
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)
        return InitiationPaiementUSSDView.as_view()(request)

    def _soumettre(self, session, **changements):
        payload = {
            'sessionReference': session.reference,
            'network': 'orange',
            'rawText': (
                'Transfert vers 690169694 réussi. '
                'ID transaction: PP260908.TEST.'
            ),
            'amount': str(session.montant_total),
            'senderName': 'CHAUFFEUR TEST',
            'senderPhone': '690458393',
            'recipientName': 'PROPRIETAIRE PRINCIPAL',
            'recipientPhone': '690169694',
            'reference': 'PP260908.TEST',
            'timestamp': None,
            'fee': '150.00',
            'commission': '0.00',
            'newBalance': '1000.00',
            'capturedAtDevice': timezone.now().isoformat(),
        }
        payload.update(changements)
        request = APIRequestFactory().post(
            '/api/v1/soumettre-preuve-paiement-ussd/',
            payload,
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)
        return SoumissionPreuvePaiementUSSDView.as_view()(request)

    def test_initiation_groupe_deux_contrats_du_meme_proprietaire(self):
        response = self._initier([
            (self.lease, Decimal('3500.00')),
            (self.lease_meme_proprietaire, Decimal('1000.00')),
        ])

        self.assertEqual(response.status_code, 201)
        session = SessionPaiement.objects.get(
            reference=response.data['reference_interne'],
        )
        self.assertEqual(
            session.canal,
            SessionPaiement.CANAL_USSD_ASSISTE,
        )
        self.assertEqual(session.proprietaire_id, self.proprietaire.id)
        self.assertEqual(
            session.compte_reception_id,
            self.compte_orange.id,
        )
        self.assertEqual(session.operateur, 'ORANGE')
        self.assertEqual(session.numero_destinataire, '237690169694')
        self.assertEqual(session.montant_total, Decimal('4500.00'))
        self.assertEqual(session.lignes_paiement.count(), 2)
        self.assertFalse(
            session.lignes_paiement.exclude(
                methode=Paiement.METHODE_USSD_ASSISTE,
                statut=Paiement.STATUT_EN_ATTENTE,
            ).exists()
        )

    def test_initiation_refuse_deux_proprietaires(self):
        response = self._initier([
            (self.lease, Decimal('3500.00')),
            (self.lease_autre_proprietaire, Decimal('2500.00')),
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            'propriétaires différents',
            response.data['dev_message'],
        )
        self.assertFalse(SessionPaiement.objects.exists())

    def test_initiation_refuse_operateur_non_configure(self):
        response = self._initier([
            (self.lease, Decimal('3500.00')),
        ], operateur='mtn')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Aucun compte MTN actif', response.data['dev_message'])
        self.assertFalse(SessionPaiement.objects.exists())

    def test_un_second_paiement_ussd_du_meme_lease_est_refuse(self):
        premiere_reponse = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        seconde_reponse = self._initier([
            (self.lease, Decimal('3500.00')),
        ])

        self.assertEqual(premiere_reponse.status_code, 201)
        self.assertEqual(seconde_reponse.status_code, 400)
        self.assertIn(
            'déjà en cours',
            seconde_reponse.data['dev_message'],
        )
        self.assertEqual(SessionPaiement.objects.count(), 1)

    def test_soumission_met_en_verification_sans_payer_le_lease(self):
        initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session = SessionPaiement.objects.get(
            reference=initiation.data['reference_interne'],
        )

        response = self._soumettre(session)

        self.assertEqual(response.status_code, 201)
        session.refresh_from_db()
        self.lease.refresh_from_db()
        paiement = session.lignes_paiement.get()
        preuve = PreuvePaiementUSSD.objects.get(session=session)

        self.assertEqual(
            session.statut,
            SessionPaiement.STATUT_EN_VERIFICATION,
        )
        self.assertEqual(
            preuve.statut,
            PreuvePaiementUSSD.STATUT_EN_VERIFICATION,
        )
        self.assertEqual(preuve.montant_transfere, Decimal('3500.00'))
        self.assertEqual(preuve.frais_operateur, Decimal('150.00'))
        self.assertEqual(self.lease.montant_paye, Decimal('0.00'))
        self.assertEqual(self.lease.statut, Lease.STATUT_NON_PAYE)
        self.assertEqual(paiement.statut, Paiement.STATUT_EN_ATTENTE)
        self.assertTrue(
            LeaseSerializer(self.lease).data[
                'paiement_en_verification'
            ]
        )

    def test_soumission_refuse_un_mauvais_destinataire(self):
        initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session = SessionPaiement.objects.get(
            reference=initiation.data['reference_interne'],
        )

        response = self._soumettre(
            session,
            recipientPhone='690000000',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('recipientPhone', response.data['dev_message'])
        session.refresh_from_db()
        self.assertEqual(session.statut, SessionPaiement.STATUT_EN_ATTENTE)
        self.assertFalse(PreuvePaiementUSSD.objects.exists())

    def test_reference_operateur_ne_peut_pas_etre_reutilisee(self):
        initiation_1 = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session_1 = SessionPaiement.objects.get(
            reference=initiation_1.data['reference_interne'],
        )
        self.assertEqual(self._soumettre(session_1).status_code, 201)

        initiation_2 = self._initier([
            (self.lease_meme_proprietaire, Decimal('1000.00')),
        ])
        session_2 = SessionPaiement.objects.get(
            reference=initiation_2.data['reference_interne'],
        )
        response = self._soumettre(session_2)

        self.assertEqual(response.status_code, 400)
        self.assertIn('reference', response.data['dev_message'])
        session_2.refresh_from_db()
        self.assertEqual(session_2.statut, SessionPaiement.STATUT_EN_ATTENTE)

    def test_validation_declenche_ventilation_et_ignore_les_frais(self):
        initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session = SessionPaiement.objects.get(
            reference=initiation.data['reference_interne'],
        )
        self.assertEqual(self._soumettre(session).status_code, 201)
        preuve = PreuvePaiementUSSD.objects.get(session=session)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            preuve, modifiee = PaiementUSSDService.valider_preuve(
                preuve_id=preuve.id,
                agent=self.utilisateur,
            )

        self.assertTrue(modifiee)
        self.assertEqual(len(callbacks), 1)
        preuve.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(
            preuve.statut,
            PreuvePaiementUSSD.STATUT_VALIDEE,
        )
        self.assertEqual(session.statut, SessionPaiement.STATUT_VALIDE)
        self.assertEqual(preuve.verifie_par_id, self.utilisateur.id)

        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(session.id, 'SUCCESS')

        self.lease.refresh_from_db()
        self.contrat.refresh_from_db()
        paiement = session.lignes_paiement.get()
        self.assertEqual(paiement.statut, Paiement.STATUT_VALIDE)
        self.assertEqual(paiement.montant, Decimal('3500.00'))
        self.assertEqual(self.lease.montant_paye, Decimal('3500.00'))
        self.assertEqual(self.lease.statut, Lease.STATUT_PAYE)
        self.assertEqual(self.contrat.montant_paye, Decimal('3500.00'))
        self.assertEqual(
            self.contrat.montant_restant,
            Decimal('196500.00'),
        )
        self.assertEqual(preuve.frais_operateur, Decimal('150.00'))

        _, modifiee_deuxieme_fois = (
            PaiementUSSDService.valider_preuve(
                preuve_id=preuve.id,
                agent=self.utilisateur,
            )
        )
        self.assertFalse(modifiee_deuxieme_fois)

    def test_rejet_libere_le_lease_sans_modifier_ses_montants(self):
        initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session = SessionPaiement.objects.get(
            reference=initiation.data['reference_interne'],
        )
        self.assertEqual(self._soumettre(session).status_code, 201)
        preuve = PreuvePaiementUSSD.objects.get(session=session)

        preuve, modifiee = PaiementUSSDService.rejeter_preuve(
            preuve_id=preuve.id,
            agent=self.utilisateur,
            motif='Transaction absente du relevé du propriétaire.',
        )

        self.assertTrue(modifiee)
        preuve.refresh_from_db()
        session.refresh_from_db()
        self.lease.refresh_from_db()
        paiement = session.lignes_paiement.get()
        self.assertEqual(
            preuve.statut,
            PreuvePaiementUSSD.STATUT_REJETEE,
        )
        self.assertEqual(
            preuve.motif_rejet,
            'Transaction absente du relevé du propriétaire.',
        )
        self.assertEqual(session.statut, SessionPaiement.STATUT_REJETE)
        self.assertEqual(paiement.statut, Paiement.STATUT_ECHEC)
        self.assertEqual(self.lease.montant_paye, Decimal('0.00'))
        self.assertEqual(self.lease.statut, Lease.STATUT_NON_PAYE)

        nouvelle_initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        self.assertEqual(nouvelle_initiation.status_code, 201)

    def test_validation_ussd_genere_le_lease_suivant(self):
        regle = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Règle USSD quotidienne',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 9, 1, 8),
            actif=True,
        )
        self.contrat.regle_generation = regle
        self.contrat.save(update_fields=['regle_generation'])

        initiation = self._initier([
            (self.lease, Decimal('3500.00')),
        ])
        session = SessionPaiement.objects.get(
            reference=initiation.data['reference_interne'],
        )
        self.assertEqual(self._soumettre(session).status_code, 201)
        preuve = PreuvePaiementUSSD.objects.get(session=session)

        with self.captureOnCommitCallbacks(execute=False):
            PaiementUSSDService.valider_preuve(
                preuve_id=preuve.id,
                agent=self.utilisateur,
            )
        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(session.id, 'SUCCESS')

        lease_suivant = Lease.objects.get(
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 9, 2, 8),
        )
        self.assertEqual(lease_suivant.statut, Lease.STATUT_NON_PAYE)
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 9, 3, 8),
        )

    def test_admin_preuve_est_en_lecture_seule_avec_actions_manuelles(self):
        preuve_admin = admin.site._registry[PreuvePaiementUSSD]

        self.assertFalse(
            preuve_admin.has_add_permission(
                SimpleNamespace(user=self.utilisateur),
            )
        )
        self.assertFalse(
            preuve_admin.has_delete_permission(
                SimpleNamespace(user=self.utilisateur),
            )
        )
        self.assertIn('valider_preuves_ussd', preuve_admin.actions)
        self.assertIn('rejeter_preuves_ussd', preuve_admin.actions)


class CalculProchaineOccurrenceTests(SimpleTestCase):
    def test_cron_de_12h_passe_a_22h_le_meme_jour(self):
        regle = SimpleNamespace(
            id=3,
            frequence=Schedule.CRON,
            cron_expression='0 12,22 * * *',
        )

        suivante = calculer_prochaine_occurrence(
            regle,
            occurrence_aware(2026, 7, 29, 12),
        )

        self.assertEqual(
            suivante,
            occurrence_aware(2026, 7, 29, 22),
        )

    def test_cron_de_22h_passe_a_12h_le_lendemain(self):
        regle = SimpleNamespace(
            id=3,
            frequence=Schedule.CRON,
            cron_expression='0 12,22 * * *',
        )

        suivante = calculer_prochaine_occurrence(
            regle,
            occurrence_aware(2026, 7, 29, 22),
        )

        self.assertEqual(
            suivante,
            occurrence_aware(2026, 7, 30, 12),
        )


class ConfigurationPaiementTests(TestCase):
    compte_id = 77

    def setUp(self):
        self.utilisateur = CustomUser.objects.create(
            keycloak_id='payment-config-user',
            compte_id=self.compte_id,
            nom_complet='Payeur configuration',
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.type_contrat = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Véhicule paiement',
            code='PAY-CONFIG',
            est_principal=True,
        )
        self.agence = Agence.objects.create(
            compte_id=self.compte_id,
            nom='Agence principale',
            code='AG-PRINCIPALE',
        )
        self.autre_agence = Agence.objects.create(
            compte_id=self.compte_id,
            nom='Autre agence',
            code='AG-AUTRE',
        )
        self.config_defaut = ConfigPaiement.objects.create(
            compte_id=self.compte_id,
            nom='Encaissement principal',
            api_key='api-key-principale',
            base_url='https://principal.paygate.test',
            success_url='https://principal.test/success',
            webhook_secret='secret-principal',
            actif=True,
            defaut=True,
        )
        self.config_speciale = ConfigPaiement.objects.create(
            compte_id=self.compte_id,
            nom='Encaissement spécial',
            api_key='api-key-speciale',
            base_url='https://special.paygate.test',
            success_url='https://special.test/success',
            webhook_secret='secret-special',
            actif=True,
        )
        self.contrat_defaut = self._creer_contrat(
            nom='Contrat configuration principale',
            config_paiement=self.config_defaut,
        )
        self.utilisateur_special = CustomUser.objects.create(
            keycloak_id='payment-config-special-driver',
            compte_id=self.compte_id,
            nom_complet='Payeur configuration spéciale',
            is_active=True,
        )
        self.contrat_special = self._creer_contrat(
            nom='Contrat configuration spéciale',
            config_paiement=self.config_speciale,
            chauffeur=self.utilisateur_special,
        )
        self.lease_defaut = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat_defaut,
            date_echeance=occurrence_aware(2026, 7, 31, 12),
            montant_attendu=Decimal('50.00'),
        )
        self.lease_special = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat_special,
            date_echeance=occurrence_aware(2026, 7, 31, 12),
            montant_attendu=Decimal('50.00'),
        )

    def _creer_contrat(
        self,
        nom,
        config_paiement,
        parent=None,
        agence=None,
        chauffeur=None,
    ):
        return Contrat.objects.create(
            compte_id=self.compte_id,
            agence=agence or self.agence,
            chauffeur=chauffeur or self.utilisateur,
            enregistre_par=self.utilisateur,
            type_contrat=self.type_contrat,
            parent=parent,
            nom_complet=nom,
            montant_total=Decimal('1000.00'),
            montant_restant=Decimal('1000.00'),
            montant_par_paiement=Decimal('50.00'),
            montant_paye=Decimal('0.00'),
            frequence=Contrat.JOURNALIER,
            date_debut=date(2026, 7, 31),
            date_fin=date(2026, 12, 31),
            prochaine_echeance=occurrence_aware(2026, 7, 31, 12),
            statut=Contrat.STATUT_ACTIF,
            config_paiement=config_paiement,
        )

    def _creer_session(self, config):
        return SessionPaiement.objects.create(
            compte_id=self.compte_id,
            agence=self.agence,
            reference=SessionPaiement.generer_reference_session(),
            gateway_reference='GATEWAY.TEST',
            montant_total=Decimal('50.00'),
            telephone='690000000',
            utilisateur=self.utilisateur,
            config_paiement=config,
            statut=SessionPaiement.STATUT_EN_ATTENTE,
        )

    def test_plusieurs_configurations_du_meme_compte_sont_autorisees(self):
        self.assertEqual(
            ConfigPaiement.objects.filter(compte_id=self.compte_id).count(),
            2,
        )

    def test_nouveau_contrat_parent_recoit_la_configuration_par_defaut(self):
        chauffeur = CustomUser.objects.create(
            keycloak_id='payment-default-driver',
            compte_id=self.compte_id,
            nom_complet='Chauffeur configuration par défaut',
            is_active=True,
        )

        contrat = self._creer_contrat(
            nom='Contrat avec défaut automatique',
            config_paiement=None,
            chauffeur=chauffeur,
        )

        self.assertEqual(contrat.config_paiement_id, self.config_defaut.id)

    def test_configuration_explicite_du_parent_reste_prioritaire(self):
        chauffeur = CustomUser.objects.create(
            keycloak_id='payment-explicit-driver',
            compte_id=self.compte_id,
            nom_complet='Chauffeur configuration explicite',
            is_active=True,
        )

        contrat = self._creer_contrat(
            nom='Contrat avec configuration explicite',
            config_paiement=self.config_speciale,
            chauffeur=chauffeur,
        )

        self.assertEqual(contrat.config_paiement_id, self.config_speciale.id)

    def test_sous_contrat_herite_toujours_de_la_configuration_parent(self):
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat héritier',
            parent=self.contrat_defaut,
            config_paiement=self.config_speciale,
        )

        self.assertEqual(
            sous_contrat.config_paiement_id,
            self.config_defaut.id,
        )

        sous_contrat.config_paiement = self.config_speciale
        sous_contrat.save(update_fields=['config_paiement'])
        sous_contrat.refresh_from_db()
        self.assertEqual(
            sous_contrat.config_paiement_id,
            self.config_defaut.id,
        )

    def test_modification_du_parent_propage_la_configuration(self):
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat à synchroniser',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
        )

        self.contrat_defaut.config_paiement = self.config_speciale
        self.contrat_defaut.save(update_fields=['config_paiement'])

        sous_contrat.refresh_from_db()
        self.assertEqual(
            sous_contrat.config_paiement_id,
            self.config_speciale.id,
        )

    def test_une_seule_configuration_par_defaut_par_compte(self):
        self.config_speciale.defaut = True
        self.config_speciale.save(update_fields=['defaut'])

        self.config_defaut.refresh_from_db()
        self.assertFalse(self.config_defaut.defaut)
        self.assertTrue(self.config_speciale.defaut)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConfigPaiement.objects.filter(
                    pk__in=[self.config_defaut.pk, self.config_speciale.pk],
                ).update(defaut=True)

    def test_compte_configuration_est_immuable(self):
        self.config_speciale.compte_id = 88
        with self.assertRaises(ValidationError):
            self.config_speciale.save()

    def test_resolution_utilise_la_configuration_affectee_au_contrat(self):
        self.assertEqual(
            PaymentService.resoudre_config_paiement_pour_leases([
                self.lease_defaut,
            ]),
            self.config_defaut,
        )
        self.assertEqual(
            PaymentService.resoudre_config_paiement_pour_leases([
                self.lease_special,
            ]),
            self.config_speciale,
        )

    def test_contrat_et_session_acceptent_une_configuration_vide(self):
        self.contrat_defaut.config_paiement = None
        self.contrat_defaut.save(update_fields=['config_paiement'])
        session = self._creer_session(None)

        self.contrat_defaut.refresh_from_db()
        self.assertIsNone(self.contrat_defaut.config_paiement_id)
        self.assertIsNone(session.config_paiement_id)

    def test_mobile_money_refuse_un_contrat_sans_configuration(self):
        self.contrat_defaut.config_paiement = None
        self.contrat_defaut.save(update_fields=['config_paiement'])

        with self.assertRaises(CustomAPIException) as contexte:
            PaymentService.resoudre_config_paiement_pour_leases([
                self.lease_defaut,
            ])

        self.assertEqual(contexte.exception.status_code, 400)
        self.assertIn('Mobile Money', contexte.exception.dev_message)

    def test_panier_de_configurations_differentes_est_refuse(self):
        with self.assertRaises(CustomAPIException) as contexte:
            PaymentService.resoudre_config_paiement_pour_leases([
                self.lease_defaut,
                self.lease_special,
            ])

        self.assertEqual(contexte.exception.status_code, 400)
        self.assertEqual(
            contexte.exception.dev_message,
            "Les échéances sélectionnées utilisent des configurations de "
            "paiement différentes. Veuillez effectuer deux paiements "
            "séparés.",
        )

    def test_configuration_inactive_est_refusee(self):
        self.config_speciale.actif = False
        self.config_speciale.save(update_fields=['actif'])

        with self.assertRaises(CustomAPIException) as contexte:
            PaymentService.resoudre_config_paiement_pour_leases([
                self.lease_special,
            ])

        self.assertEqual(contexte.exception.status_code, 400)
        self.assertIn('inactive', contexte.exception.dev_message)

    def test_action_admin_affecte_une_configuration_du_meme_compte(self):
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat action administration',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
        )
        formulaire = AssignerConfigPaiementContratsForm(
            compte_id=self.compte_id,
        )
        self.assertFalse(formulaire.fields['config_paiement'].required)
        formulaire_sans_config = AssignerConfigPaiementContratsForm(
            data={'config_paiement': ''},
            compte_id=self.compte_id,
        )
        self.assertTrue(formulaire_sans_config.is_valid())
        self.assertIsNone(
            formulaire_sans_config.cleaned_data['config_paiement']
        )
        self.assertEqual(
            set(
                formulaire.fields['config_paiement']
                .queryset
                .values_list('id', flat=True)
            ),
            {self.config_defaut.id, self.config_speciale.id},
        )

        request = RequestFactory().post(
            '/admin/recouvrement/contrat/',
            {
                'action': 'assigner_config_paiement',
                ACTION_CHECKBOX_NAME: [self.contrat_defaut.id],
                'config_paiement': self.config_speciale.id,
                'appliquer_config_paiement': '1',
            },
        )
        contrat_admin = admin.site._registry[Contrat]
        with patch.object(contrat_admin, 'message_user'):
            resultat = contrat_admin.assigner_config_paiement(
                request,
                Contrat.objects.filter(pk=self.contrat_defaut.pk),
            )

        self.assertIsNone(resultat)
        self.contrat_defaut.refresh_from_db()
        self.assertEqual(
            self.contrat_defaut.config_paiement_id,
            self.config_speciale.id,
        )
        sous_contrat.refresh_from_db()
        self.assertEqual(
            sous_contrat.config_paiement_id,
            self.config_speciale.id,
        )

    def test_action_admin_refuse_un_sous_contrat(self):
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat action interdite',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
        )
        request = RequestFactory().post(
            '/admin/recouvrement/contrat/',
            {
                'action': 'assigner_config_paiement',
                ACTION_CHECKBOX_NAME: [sous_contrat.id],
                'config_paiement': self.config_speciale.id,
                'appliquer_config_paiement': '1',
            },
        )
        contrat_admin = admin.site._registry[Contrat]

        with patch.object(contrat_admin, 'message_user') as message_user:
            resultat = contrat_admin.assigner_config_paiement(
                request,
                Contrat.objects.filter(pk=sous_contrat.pk),
            )

        self.assertIsNone(resultat)
        sous_contrat.refresh_from_db()
        self.assertEqual(
            sous_contrat.config_paiement_id,
            self.config_defaut.id,
        )
        self.assertIn(
            'uniquement depuis les contrats principaux',
            message_user.call_args.args[1],
        )

    def test_admin_rend_configuration_sous_contrat_non_modifiable(self):
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat lecture seule',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
        )
        contrat_admin = admin.site._registry[Contrat]

        champs = contrat_admin.get_readonly_fields(
            SimpleNamespace(user=self.utilisateur),
            sous_contrat,
        )

        self.assertIn('config_paiement', champs)
        self.assertNotIn(
            'config_paiement',
            contrat_admin.get_readonly_fields(
                SimpleNamespace(user=self.utilisateur),
                self.contrat_defaut,
            ),
        )

    def test_api_expose_configuration_sans_autoriser_sa_modification(self):
        request = SimpleNamespace(
            user=self.utilisateur,
            data={'config_paiement': self.config_speciale.id},
        )
        serializer = ContratSerializer(
            self.contrat_defaut,
            data={'config_paiement': self.config_speciale.id},
            partial=True,
            context={'request': request},
        )

        self.assertTrue(serializer.fields['config_paiement'].read_only)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.contrat_defaut.refresh_from_db()
        self.assertEqual(
            self.contrat_defaut.config_paiement_id,
            self.config_defaut.id,
        )
        self.assertTrue(
            SousContratSerializer().fields['config_paiement'].read_only
        )

    def test_configuration_par_defaut_est_visible_dans_admin(self):
        config_admin = admin.site._registry[ConfigPaiement]
        self.assertIn('defaut', config_admin.list_display)
        self.assertIn('defaut', config_admin.list_filter)
        champs_fieldsets = {
            champ
            for _, options in config_admin.fieldsets
            for champ in options['fields']
        }
        self.assertIn('defaut', champs_fieldsets)

    def test_initiation_memorise_la_configuration_sur_la_session(self):
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement/',
            {
                'lignes': [{
                    'lease_id': self.lease_special.id,
                    'montant': '50.00',
                }],
                'phone_number': '690000000',
            },
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)

        with (
            patch(
                'recouvrement.api.v1.views.PaymentService.'
                'traiter_paiement_complet',
                return_value={
                    'paygate_reference': 'PAYGATE.CONFIG.TEST',
                    'session_token': 'token-test',
                    'collect_ok': True,
                    'collect_error': None,
                },
            ) as traiter,
            patch('recouvrement.api.v1.views._schedule_next_verification'),
        ):
            response = InitiationPaiementView.as_view()(request)

        self.assertEqual(response.status_code, 201)
        session = SessionPaiement.objects.get(
            reference=response.data['reference_interne'],
        )
        self.assertEqual(
            session.config_paiement_id,
            self.config_speciale.id,
        )
        self.assertEqual(session.compte_id, self.contrat_special.compte_id)
        self.assertEqual(session.agence_id, self.agence.id)
        self.assertEqual(
            session.lignes_paiement.get().agence_id,
            self.agence.id,
        )
        self.assertEqual(
            traiter.call_args.kwargs['config_paiement_id'],
            self.config_speciale.id,
        )

    def test_initiation_refuse_des_leases_de_differentes_agences(self):
        contrat_autre_agence = self._creer_contrat(
            nom='Sous-contrat autre agence',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
            agence=self.agence,
        )
        # Le modèle impose normalement l'agence du parent. On simule ici
        # une ancienne donnée incohérente pour vérifier le garde-fou API.
        Contrat.objects.filter(pk=contrat_autre_agence.pk).update(
            agence=self.autre_agence,
        )
        contrat_autre_agence.refresh_from_db()
        lease_autre_agence = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=contrat_autre_agence,
            date_echeance=occurrence_aware(2026, 7, 31, 12),
            montant_attendu=Decimal('50.00'),
        )
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement/',
            {
                'lignes': [
                    {'lease_id': self.lease_defaut.id, 'montant': '10.00'},
                    {'lease_id': lease_autre_agence.id, 'montant': '10.00'},
                ],
                'phone_number': '690000000',
            },
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)

        nombre_sessions_avant = SessionPaiement.objects.count()
        response = InitiationPaiementView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            'paiements séparés par agence',
            response.data['dev_message'],
        )
        self.assertEqual(
            SessionPaiement.objects.count(),
            nombre_sessions_avant,
        )

    def test_initiation_refuse_un_contrat_sans_agence(self):
        self.contrat_defaut.agence = None
        self.contrat_defaut.save(update_fields=['agence'])
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement/',
            {
                'lignes': [{
                    'lease_id': self.lease_defaut.id,
                    'montant': '10.00',
                }],
                'phone_number': '690000000',
            },
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)

        nombre_sessions_avant = SessionPaiement.objects.count()
        response = InitiationPaiementView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn('rattaché à une agence', response.data['dev_message'])
        self.assertEqual(
            SessionPaiement.objects.count(),
            nombre_sessions_avant,
        )

    def test_initiation_mixte_retourne_400_sans_creer_de_session(self):
        sous_contrat_incoherent = self._creer_contrat(
            nom='Ancien sous-contrat incohérent',
            parent=self.contrat_defaut,
            config_paiement=self.config_defaut,
        )
        # Simule une donnée historique antérieure à l'invariant modèle. Le
        # garde-fou du service doit encore empêcher le mélange financier.
        Contrat.objects.filter(pk=sous_contrat_incoherent.pk).update(
            config_paiement=self.config_speciale,
        )
        lease_incoherent = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=sous_contrat_incoherent,
            date_echeance=occurrence_aware(2026, 7, 31, 12),
            montant_attendu=Decimal('50.00'),
        )
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement/',
            {
                'lignes': [
                    {'lease_id': self.lease_defaut.id, 'montant': '10.00'},
                    {'lease_id': lease_incoherent.id, 'montant': '10.00'},
                ],
                'phone_number': '690000000',
            },
            format='json',
        )
        force_authenticate(request, user=self.utilisateur)

        nombre_sessions_avant = SessionPaiement.objects.count()
        response = InitiationPaiementView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data['dev_message'],
            "Les échéances sélectionnées utilisent des configurations de "
            "paiement différentes. Veuillez effectuer deux paiements "
            "séparés.",
        )
        self.assertEqual(
            SessionPaiement.objects.count(),
            nombre_sessions_avant,
        )

    def _creer_famille_sans_historique(self):
        chauffeur = CustomUser.objects.create(
            keycloak_id=f'family-driver-{CustomUser.objects.count()}',
            compte_id=self.compte_id,
            nom_complet='Chauffeur famille agence',
            is_active=True,
        )
        parent = self._creer_contrat(
            nom='Parent sans historique',
            config_paiement=self.config_defaut,
            agence=self.agence,
            chauffeur=chauffeur,
        )
        sous_contrat = self._creer_contrat(
            nom='Sous-contrat sans historique',
            config_paiement=self.config_defaut,
            parent=parent,
            agence=self.agence,
            chauffeur=chauffeur,
        )
        return parent, sous_contrat

    def test_api_refuse_une_agence_differente_sur_un_sous_contrat(self):
        _, sous_contrat = self._creer_famille_sans_historique()
        request = SimpleNamespace(
            user=self.utilisateur,
            data={'agence': self.autre_agence.id},
        )
        serializer = ContratSerializer(
            sous_contrat,
            data={'agence': self.autre_agence.id},
            partial=True,
            context={'request': request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('agence', serializer.errors)

    def test_api_propage_agence_du_parent_sans_historique(self):
        parent, sous_contrat = self._creer_famille_sans_historique()
        request = SimpleNamespace(
            user=self.utilisateur,
            data={'agence': self.autre_agence.id},
        )
        serializer = ContratSerializer(
            parent,
            data={'agence': self.autre_agence.id},
            partial=True,
            context={'request': request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        sous_contrat.refresh_from_db()
        self.assertEqual(sous_contrat.agence_id, self.autre_agence.id)

    def test_admin_agence_facultative_et_propagee_sans_historique(self):
        parent, sous_contrat = self._creer_famille_sans_historique()
        donnees = model_to_dict(parent)
        donnees['agence'] = self.autre_agence.id
        formulaire = ContratAdminForm(data=donnees, instance=parent)

        self.assertFalse(formulaire.fields['agence'].required)
        self.assertTrue(formulaire.is_valid(), formulaire.errors)
        contrat_modifie = formulaire.save(commit=False)
        contrat_admin = admin.site._registry[Contrat]
        contrat_admin.save_model(
            SimpleNamespace(user=self.utilisateur),
            contrat_modifie,
            formulaire,
            change=True,
        )

        sous_contrat.refresh_from_db()
        self.assertEqual(sous_contrat.agence_id, self.autre_agence.id)

    def test_admin_bloque_changement_si_un_sous_contrat_a_un_lease(self):
        parent, sous_contrat = self._creer_famille_sans_historique()
        Lease.objects.create(
            compte_id=self.compte_id,
            contrat=sous_contrat,
            date_echeance=occurrence_aware(2026, 8, 1, 12),
            montant_attendu=Decimal('50.00'),
        )
        donnees = model_to_dict(parent)
        donnees['agence'] = self.autre_agence.id
        formulaire = ContratAdminForm(data=donnees, instance=parent)

        self.assertFalse(formulaire.is_valid())
        self.assertIn('agence', formulaire.errors)

    def test_serializers_exposent_agence_et_nom(self):
        session = self._creer_session(self.config_defaut)
        paiement = Paiement.objects.create(
            compte_id=self.compte_id,
            agence=self.agence,
            contrat=self.contrat_defaut,
            lease=self.lease_defaut,
            enregistre_par=self.utilisateur,
            session=session,
            montant=Decimal('10.00'),
            methode=Paiement.METHODE_MOBILE_MONEY,
        )
        penalite = Penalite.objects.create(
            compte_id=self.compte_id,
            agence=self.agence,
            lease=self.lease_defaut,
            nom_complet=self.contrat_defaut.nom_complet,
            montant=Decimal('5.00'),
            motif='Test agence serializer',
        )

        representations = [
            LeaseSerializer(self.lease_defaut).data,
            PaiementSerializer(paiement).data,
            PenaliteSerializer(penalite).data,
            SessionPaiementSerializer(session).data,
        ]
        for representation in representations:
            self.assertEqual(representation['agence'], self.agence.id)
            self.assertEqual(representation['agence_nom'], self.agence.nom)

    def test_webhook_utilise_le_secret_memorise_sur_la_session(self):
        session = self._creer_session(self.config_speciale)
        payload = {
            'external_reference': session.reference,
            'reference': session.gateway_reference,
            'status': 'PENDING',
        }
        corps = json.dumps(payload).encode('utf-8')
        signature = hmac.new(
            self.config_speciale.webhook_secret.encode('utf-8'),
            corps,
            hashlib.sha256,
        ).hexdigest()
        request = APIRequestFactory().generic(
            'POST',
            '/api/v1/webhook/',
            corps,
            content_type='application/json',
            HTTP_X_SIGNATURE=signature,
        )

        response = WebhookView.as_view()(request)

        self.assertEqual(response.status_code, 200)

    def test_polling_utilise_la_configuration_memorisee(self):
        session = self._creer_session(self.config_speciale)
        with (
            patch(
                'core.tasks.PaymentService.verifier_statut_transaction',
                return_value={'status': 'PENDING'},
            ) as verifier,
            patch('core.tasks._schedule_next_verification'),
        ):
            verifier_statut_session_task(session.id)

        self.assertEqual(
            verifier.call_args.kwargs['config_paiement_id'],
            self.config_speciale.id,
        )


class AdministrationIdCompteTests(SimpleTestCase):
    def test_tous_les_admins_avec_compte_affichent_id_compte_en_premier(self):
        admins_concernes = [
            model_admin
            for model, model_admin in admin.site._registry.items()
            if any(
                champ.name == 'compte_id'
                for champ in model._meta.fields
            )
        ]

        self.assertEqual(len(admins_concernes), 16)
        for model_admin in admins_concernes:
            list_display = tuple(model_admin.list_display)
            self.assertEqual(
                list_display[0],
                'id_compte',
                model_admin.model._meta.label,
            )
            self.assertNotIn(
                'compte_id',
                list_display,
                model_admin.model._meta.label,
            )

    def test_colonne_id_compte_reproduit_le_rendu_attendu(self):
        lease_admin = admin.site._registry[Lease]

        contenu = str(lease_admin.id_compte(
            SimpleNamespace(pk=5, compte_id=2),
        ))

        self.assertIn('<strong>#5</strong>', contenu)
        self.assertIn('Compte 2', contenu)


class AdministrationGenerationLeaseTests(TestCase):
    def test_regle_generation_est_enregistree_dans_admin(self):
        self.assertTrue(
            admin.site.is_registered(RegleGenerationLease)
        )

    def test_regle_par_defaut_est_visible_dans_admin(self):
        regle_admin = admin.site._registry[RegleGenerationLease]
        self.assertIn('defaut', regle_admin.list_display)
        self.assertIn('defaut', regle_admin.list_filter)
        champs_fieldsets = {
            champ
            for _, options in regle_admin.fieldsets
            for champ in options['fields']
        }
        self.assertIn('defaut', champs_fieldsets)

    def test_admin_rejette_une_expression_cron_invalide(self):
        formulaire = RegleGenerationLeaseAdminForm(data={
            'compte_id': 77,
            'nom': 'Règle admin test',
            'frequence': Schedule.CRON,
            'cron_expression': 'expression invalide',
            'debut': '2026-07-30 12:00:00',
            'actif': True,
        })

        self.assertFalse(formulaire.is_valid())
        self.assertIn('cron_expression', formulaire.errors)

    def test_admin_rejette_la_regle_generation_d_un_autre_compte(self):
        regle_autre_compte = RegleGenerationLease.objects.create(
            compte_id=88,
            nom='Règle autre compte',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 7, 30, 12),
            actif=True,
        )
        formulaire = ContratAdminForm(data={
            'compte_id': 77,
            'regle_generation': regle_autre_compte.id,
        })

        self.assertFalse(formulaire.is_valid())
        self.assertIn(
            'même compte',
            formulaire.errors['regle_generation'][0],
        )

    def test_action_ne_propose_que_les_regles_du_compte_selectionne(self):
        regle_compte = RegleGenerationLease.objects.create(
            compte_id=77,
            nom='Règle du compte sélectionné',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 7, 30, 12),
            actif=True,
        )
        RegleGenerationLease.objects.create(
            compte_id=88,
            nom='Règle autre compte',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 7, 30, 12),
            actif=True,
        )

        formulaire = AssignerRegleGenerationContratsForm(
            compte_id=77,
        )

        self.assertEqual(
            list(
                formulaire.fields['regle_generation']
                .queryset
                .values_list('id', flat=True)
            ),
            [regle_compte.id],
        )


class AssignationRegleGenerationTests(TestCase):
    compte_id = 77
    autre_compte_id = 88

    def setUp(self):
        self.agent = CustomUser.objects.create(
            keycloak_id='agent-assignation-regle',
            compte_id=self.compte_id,
            nom_complet='Agent assignation règle',
            is_active=True,
        )
        self.superuser = CustomUser.objects.create(
            keycloak_id='superuser-assignation-regle',
            compte_id=999,
            nom_complet='Super administrateur règles',
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.chauffeur = CustomUser.objects.create(
            keycloak_id='driver-assignation-regle',
            compte_id=self.compte_id,
            nom_complet='Chauffeur règle',
            is_active=True,
        )
        self.autre_chauffeur = CustomUser.objects.create(
            keycloak_id='driver-autre-compte-regle',
            compte_id=self.autre_compte_id,
            nom_complet='Chauffeur autre compte',
            is_active=True,
        )
        self.type_parent = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Moto assignation règle',
            code='MOTO-ASSIGN',
            est_principal=True,
        )
        self.type_sous_contrat = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Care assignation règle',
            code='CARE-ASSIGN',
            est_principal=False,
        )
        self.type_autre_compte = TypeContrat.objects.create(
            compte_id=self.autre_compte_id,
            libelle='Moto autre compte',
            code='MOTO-AUTRE',
            est_principal=True,
        )
        self.regle_defaut = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Règle par défaut tests',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 9, 1, 8),
            actif=True,
            defaut=True,
        )
        self.regle_specifique = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Règle spécifique tests',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 9, 1, 12),
            actif=True,
        )
        self.regle_autre_compte = RegleGenerationLease.objects.create(
            compte_id=self.autre_compte_id,
            nom='Règle autre compte tests',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 9, 1, 10),
            actif=True,
            defaut=True,
        )
        self.contrat_defaut = self._creer_contrat(
            chauffeur=self.chauffeur,
            type_contrat=self.type_parent,
            nom='Contrat avec règle par défaut',
        )
        self.sous_contrat = self._creer_contrat(
            chauffeur=self.chauffeur,
            type_contrat=self.type_sous_contrat,
            nom='Sous-contrat indépendant',
            parent=self.contrat_defaut,
            regle_generation=self.regle_specifique,
        )
        self.contrat_autre_compte = self._creer_contrat(
            compte_id=self.autre_compte_id,
            chauffeur=self.autre_chauffeur,
            type_contrat=self.type_autre_compte,
            nom='Contrat autre compte',
        )

    def _creer_contrat(
        self,
        *,
        chauffeur,
        type_contrat,
        nom,
        compte_id=None,
        parent=None,
        regle_generation=None,
        statut=Contrat.STATUT_ACTIF,
    ):
        compte_id = compte_id or self.compte_id
        donnees = {
            'compte_id': compte_id,
            'chauffeur': chauffeur,
            'enregistre_par': self.agent,
            'type_contrat': type_contrat,
            'parent': parent,
            'nom_complet': nom,
            'montant_total': Decimal('100000.00'),
            'montant_restant': Decimal('100000.00'),
            'montant_par_paiement': Decimal('5000.00'),
            'montant_paye': Decimal('0.00'),
            'frequence': Contrat.JOURNALIER,
            'date_debut': date(2026, 9, 1),
            'date_fin': date(2026, 12, 31),
            'prochaine_echeance': occurrence_aware(2026, 9, 1, 8),
            'statut': statut,
        }
        if regle_generation is not None:
            donnees['regle_generation'] = regle_generation
        return Contrat.objects.create(**donnees)

    def _vue_assignation(self):
        return RegleGenerationLeaseViewSet.as_view(
            {'post': 'assigner_contrats'},
            **RegleGenerationLeaseViewSet.assigner_contrats.kwargs,
        )

    def _vue_planifications(self):
        return RegleGenerationLeaseViewSet.as_view(
            {'get': 'planifications'},
            **RegleGenerationLeaseViewSet.planifications.kwargs,
        )

    def _vue_planification(self):
        return RegleGenerationLeaseViewSet.as_view(
            {'get': 'planification'},
            **RegleGenerationLeaseViewSet.planification.kwargs,
        )

    def _vue_execution_immediate(self):
        return RegleGenerationLeaseViewSet.as_view(
            {'post': 'executer_maintenant'},
            **RegleGenerationLeaseViewSet.executer_maintenant.kwargs,
        )

    def _autoriser_planification(self, utilisateur, modification=False):
        codenames = ['view_reglegenerationlease']
        if modification:
            codenames.append('change_reglegenerationlease')

        role = Role.objects.create(
            libelle='Gestionnaire planification des règles',
            slug=(
                'GESTIONNAIRE_EXECUTION_REGLES'
                if modification
                else 'CONSULTATION_PLANIFICATION_REGLES'
            ),
        )
        role.permissions.add(*[
            Permission.objects.get(
                content_type__app_label='recouvrement',
                codename=codename,
            )
            for codename in codenames
        ])
        CustomUserRole.objects.create(
            compte_id=utilisateur.compte_id,
            user=utilisateur,
            role=role,
            principal=True,
            actif=True,
        )
        utilisateur._perm_cache = set(codenames)

    def _autoriser_assignation(self, utilisateur):
        role = Role.objects.create(
            libelle='Gestionnaire assignation règles',
            slug='GESTIONNAIRE_ASSIGNATION_REGLES',
        )
        role.permissions.add(
            Permission.objects.get(
                content_type__app_label='recouvrement',
                codename='view_reglegenerationlease',
            ),
            Permission.objects.get(
                content_type__app_label='recouvrement',
                codename='change_contrat',
            ),
        )
        CustomUserRole.objects.create(
            compte_id=utilisateur.compte_id,
            user=utilisateur,
            role=role,
            principal=True,
            actif=True,
        )
        utilisateur._perm_cache = {
            'view_reglegenerationlease',
            'change_contrat',
        }

    def test_nouveau_contrat_recoit_la_regle_par_defaut(self):
        self.assertEqual(
            self.contrat_defaut.regle_generation_id,
            self.regle_defaut.id,
        )

    def test_planifications_ne_liste_que_les_regles_du_compte(self):
        self._autoriser_planification(self.agent)
        request = APIRequestFactory().get(
            '/api/v1/regles-gereration-leases/planifications/',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_planifications()(request)

        self.assertEqual(response.status_code, 200)
        planifications = (
            response.data.get('results', response.data)
            if isinstance(response.data, dict)
            else response.data
        )
        regle_ids = {
            planification['regle_id']
            for planification in planifications
        }
        self.assertEqual(
            regle_ids,
            {self.regle_defaut.id, self.regle_specifique.id},
        )
        self.assertNotIn(self.regle_autre_compte.id, regle_ids)
        self.assertTrue(all(
            planification['etat_planification'] == 'PLANIFIEE'
            for planification in planifications
        ))
        self.assertTrue(all(
            planification['statut_derniere_execution'] is None
            for planification in planifications
        ))
        self.assertTrue(all(
            'derniere_execution' not in planification
            for planification in planifications
        ))

    def test_detail_planification_utilise_le_groupe_et_filtre_resultat(self):
        self._autoriser_planification(self.agent)
        nom_schedule = (
            f'regle_generation_lease_{self.regle_defaut.id}'
        )
        maintenant = timezone.now()
        Task.objects.create(
            id='a' * 32,
            name='generation-regle-test',
            func='core.tasks.generer_leases_task',
            args=(self.regle_defaut.id,),
            kwargs={},
            result={
                'regle_id': self.regle_defaut.id,
                'contrats_cibles': 3,
                'leases_crees': 2,
                'erreurs': 0,
                'detail_interne': 'ne doit pas être exposé',
            },
            group=nom_schedule,
            started=maintenant - timedelta(seconds=2),
            stopped=maintenant,
            success=True,
        )
        Task.objects.create(
            id='b' * 32,
            name='generation-autre-regle-test',
            func='core.tasks.generer_leases_task',
            args=(self.regle_autre_compte.id,),
            kwargs={},
            result={'leases_crees': 99},
            group=f'{nom_schedule}0',
            started=maintenant - timedelta(seconds=1),
            stopped=maintenant,
            success=True,
        )
        request = APIRequestFactory().get(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_defaut.id}/planification/',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_planification()(
            request,
            pk=self.regle_defaut.id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['historique_executions']), 1)
        execution = response.data['historique_executions'][0]
        self.assertEqual(execution['task_id'], 'a' * 32)
        self.assertEqual(execution['duree_secondes'], 2)
        self.assertEqual(execution['resultat']['leases_crees'], 2)
        self.assertNotIn('detail_interne', execution['resultat'])
        self.assertEqual(response.data['last_run'], maintenant)
        self.assertEqual(
            response.data['statut_derniere_execution'],
            'SUCCES',
        )
        self.assertNotIn('derniere_execution', response.data)

    def test_detail_planification_inactive_reste_consultable(self):
        self._autoriser_planification(self.agent)
        self.regle_defaut.actif = False
        self.regle_defaut.save(update_fields=['actif'])
        request = APIRequestFactory().get(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_defaut.id}/planification/',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_planification()(
            request,
            pk=self.regle_defaut.id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['etat_planification'], 'INACTIVE')
        self.assertIsNone(response.data['next_run'])
        self.assertEqual(response.data['historique_executions'], [])

    def test_execution_immediate_ne_modifie_pas_next_run(self):
        self._autoriser_planification(self.agent, modification=True)
        nom_schedule = (
            f'regle_generation_lease_{self.regle_defaut.id}'
        )
        schedule = Schedule.objects.get(name=nom_schedule)
        next_run_initial = schedule.next_run
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_defaut.id}/executer-maintenant/',
            {},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        with patch(
            'recouvrement.api.v1.views.async_task',
            return_value='task-id-generation-manuelle',
        ) as lancer_tache:
            response = self._vue_execution_immediate()(
                request,
                pk=self.regle_defaut.id,
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.data['task_id'],
            'task-id-generation-manuelle',
        )
        self.assertFalse(response.data['planification_modifiee'])
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_run, next_run_initial)
        self.assertEqual(
            lancer_tache.call_args.args[0],
            'core.tasks.generer_leases_task',
        )
        self.assertEqual(
            lancer_tache.call_args.args[1],
            self.regle_defaut.id,
        )
        self.assertEqual(
            lancer_tache.call_args.kwargs['q_options']['group'],
            nom_schedule,
        )

    def test_execution_immediate_exige_permission_de_modification(self):
        self._autoriser_planification(self.agent, modification=False)
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_defaut.id}/executer-maintenant/',
            {},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        with patch('recouvrement.api.v1.views.async_task') as lancer_tache:
            response = self._vue_execution_immediate()(
                request,
                pk=self.regle_defaut.id,
            )

        self.assertEqual(response.status_code, 403)
        lancer_tache.assert_not_called()

    def test_execution_immediate_refuse_regle_inactive(self):
        self._autoriser_planification(self.agent, modification=True)
        self.regle_defaut.actif = False
        self.regle_defaut.save(update_fields=['actif'])
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_defaut.id}/executer-maintenant/',
            {},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        with patch('recouvrement.api.v1.views.async_task') as lancer_tache:
            response = self._vue_execution_immediate()(
                request,
                pk=self.regle_defaut.id,
            )

        self.assertEqual(response.status_code, 400)
        lancer_tache.assert_not_called()

    def test_detail_planification_autre_compte_est_introuvable(self):
        self._autoriser_planification(self.agent)
        request = APIRequestFactory().get(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_autre_compte.id}/planification/',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_planification()(
            request,
            pk=self.regle_autre_compte.id,
        )

        self.assertEqual(response.status_code, 404)

    def test_parent_et_sous_contrat_conservent_des_regles_independantes(self):
        self.assertEqual(
            self.contrat_defaut.regle_generation_id,
            self.regle_defaut.id,
        )
        self.assertEqual(
            self.sous_contrat.regle_generation_id,
            self.regle_specifique.id,
        )

    def test_sous_contrat_sans_regle_ne_copie_pas_celle_du_parent(self):
        chauffeur = CustomUser.objects.create(
            keycloak_id='driver-regles-independantes',
            compte_id=self.compte_id,
            nom_complet='Chauffeur règles indépendantes',
            is_active=True,
        )
        parent = self._creer_contrat(
            chauffeur=chauffeur,
            type_contrat=self.type_parent,
            nom='Parent avec règle spécifique',
            regle_generation=self.regle_specifique,
        )
        sous_contrat = self._creer_contrat(
            chauffeur=chauffeur,
            type_contrat=self.type_sous_contrat,
            nom='Sous-contrat avec règle par défaut',
            parent=parent,
        )

        self.assertEqual(
            parent.regle_generation_id,
            self.regle_specifique.id,
        )
        self.assertEqual(
            sous_contrat.regle_generation_id,
            self.regle_defaut.id,
        )

    def test_serializers_exposent_et_cloisonnent_la_regle_generation(self):
        self.assertIn('regle_generation', ContratSerializer().fields)
        self.assertIn('regle_generation', SousContratSerializer().fields)

        request = SimpleNamespace(user=self.agent, data={})
        serializer = ContratSerializer(
            self.contrat_defaut,
            data={'regle_generation': self.regle_autre_compte.id},
            partial=True,
            context={'request': request},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('regle_generation', serializer.errors)

    def test_postgresql_garantit_un_seul_defaut_par_compte(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RegleGenerationLease.objects.filter(
                    pk=self.regle_specifique.pk,
                ).update(defaut=True)

    def test_assignation_accepte_change_contrat_sans_permission_add_regle(self):
        self._autoriser_assignation(self.agent)
        date_ancienne = occurrence_aware(2025, 1, 1, 8)
        Contrat.objects.filter(pk=self.contrat_defaut.pk).update(
            updated_at=date_ancienne,
        )
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_specifique.id}/assigner-contrats/',
            {'contrat_ids': [self.contrat_defaut.id]},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_assignation()(
            request,
            pk=self.regle_specifique.id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['contrats_mis_a_jour'], 1)
        self.contrat_defaut.refresh_from_db()
        self.assertEqual(
            self.contrat_defaut.regle_generation_id,
            self.regle_specifique.id,
        )
        self.assertGreater(self.contrat_defaut.updated_at, date_ancienne)

    def test_assignation_est_idempotente_et_ne_change_pas_updated_at(self):
        self._autoriser_assignation(self.agent)
        self.contrat_defaut.regle_generation = self.regle_specifique
        self.contrat_defaut.save(update_fields=['regle_generation'])
        date_ancienne = occurrence_aware(2025, 1, 1, 8)
        Contrat.objects.filter(pk=self.contrat_defaut.pk).update(
            updated_at=date_ancienne,
        )
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_specifique.id}/assigner-contrats/',
            {'contrat_ids': [self.contrat_defaut.id]},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_assignation()(
            request,
            pk=self.regle_specifique.id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['contrats_mis_a_jour'], 0)
        self.contrat_defaut.refresh_from_db()
        self.assertEqual(self.contrat_defaut.updated_at, date_ancienne)

    def test_superuser_ne_peut_pas_croiser_les_comptes(self):
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_specifique.id}/assigner-contrats/',
            {
                'contrat_ids': [
                    self.contrat_defaut.id,
                    self.contrat_autre_compte.id,
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.superuser)

        response = self._vue_assignation()(
            request,
            pk=self.regle_specifique.id,
        )

        self.assertEqual(response.status_code, 400)
        self.contrat_defaut.refresh_from_db()
        self.contrat_autre_compte.refresh_from_db()
        self.assertEqual(
            self.contrat_defaut.regle_generation_id,
            self.regle_defaut.id,
        )
        self.assertEqual(
            self.contrat_autre_compte.regle_generation_id,
            self.regle_autre_compte.id,
        )

    def test_api_refuse_assignation_sur_contrat_non_actif(self):
        self._autoriser_assignation(self.agent)
        self.contrat_defaut.statut = Contrat.STATUT_SUSPENDU
        self.contrat_defaut.save(update_fields=['statut'])
        request = APIRequestFactory().post(
            f'/api/v1/regles-gereration-leases/'
            f'{self.regle_specifique.id}/assigner-contrats/',
            {'contrat_ids': [self.contrat_defaut.id]},
            format='json',
        )
        force_authenticate(request, user=self.agent)

        response = self._vue_assignation()(
            request,
            pk=self.regle_specifique.id,
        )

        self.assertEqual(response.status_code, 400)
        self.contrat_defaut.refresh_from_db()
        self.assertEqual(
            self.contrat_defaut.regle_generation_id,
            self.regle_defaut.id,
        )


class GenerationLeasesTests(TestCase):
    compte_id = 77

    def setUp(self):
        self.chauffeur = CustomUser.objects.create(
            keycloak_id='driver-generation-tests',
            compte_id=self.compte_id,
            nom_complet='Chauffeur Test',
            is_active=True,
        )
        self.type_contrat = TypeContrat.objects.create(
            compte_id=self.compte_id,
            libelle='Véhicule test',
            code='VEH-TEST',
            est_principal=True,
        )
        self.config_paiement = ConfigPaiement.objects.create(
            compte_id=self.compte_id,
            nom='Encaissement generation tests',
            api_key='api-key-generation',
            base_url='https://generation.paygate.test',
            success_url='https://generation.test/success',
            webhook_secret='secret-generation',
            actif=True,
        )
        self.regle = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Deux fois par jour - tests',
            frequence=Schedule.CRON,
            cron_expression='0 12,22 * * *',
            debut=occurrence_aware(2026, 7, 29, 12),
            actif=True,
        )
        self.contrat = Contrat.objects.create(
            compte_id=self.compte_id,
            chauffeur=self.chauffeur,
            enregistre_par=self.chauffeur,
            type_contrat=self.type_contrat,
            nom_complet=self.chauffeur.nom_complet,
            montant_total=Decimal('100000.00'),
            montant_restant=Decimal('100000.00'),
            montant_par_paiement=Decimal('5000.00'),
            montant_paye=Decimal('0.00'),
            frequence=Contrat.JOURNALIER,
            date_debut=date(2026, 7, 29),
            date_fin=date(2026, 8, 31),
            prochaine_echeance=occurrence_aware(2026, 7, 29, 12),
            statut=Contrat.STATUT_ACTIF,
            regle_generation=self.regle,
            config_paiement=self.config_paiement,
        )

    def test_rattrapage_jusqua_22h_genere_deux_occurrences(self):
        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 7, 29, 22),
        )

        self.assertEqual(resultat['leases_crees'], 2)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .order_by('date_echeance')
                .values_list('date_echeance', flat=True)
            ),
            [
                occurrence_aware(2026, 7, 29, 12),
                occurrence_aware(2026, 7, 29, 22),
            ],
        )

        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 7, 30, 12),
        )

    def test_les_reponses_mobiles_exposent_temporairement_des_dates(self):
        lease = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 7, 29, 12),
            montant_attendu=Decimal('5000.00'),
        )

        self.assertEqual(
            ContratSerializer(self.contrat).data['prochaine_echeance'],
            '2026-07-29',
        )
        self.assertEqual(
            LeaseSerializer(lease).data['date_echeance'],
            '2026-07-29',
        )
        self.assertEqual(
            CalendrierSerializer(lease).data['date_echeance'],
            '2026-07-29',
        )

        valeur_entree = (
            ContratSerializer()
            .fields['prochaine_echeance']
            .run_validation('2026-07-29T22:00:00+01:00')
        )
        self.assertIsInstance(valeur_entree, datetime)
        self.assertEqual(valeur_entree.hour, 22)

    def test_q2_ne_duplique_pas_une_occurrence_generee_manuellement(self):
        limite = occurrence_aware(2026, 7, 29, 22)
        generer_leases_pour_regle(self.regle.id, limite)

        resultat_q2 = generer_leases_task(
            regle_id=self.regle.id,
            jusqu_a=limite,
        )

        self.assertEqual(resultat_q2['leases_crees'], 0)
        self.assertEqual(
            Lease.objects.filter(contrat=self.contrat).count(),
            2,
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 7, 30, 12),
        )

    def test_commande_jusqua_utilise_le_meme_moteur(self):
        sortie = StringIO()

        call_command(
            'generer_leases',
            regle=self.regle.id,
            jusqua='2026-07-29T22:00:00',
            stdout=sortie,
        )

        self.assertEqual(
            Lease.objects.filter(contrat=self.contrat).count(),
            2,
        )
        self.assertIn('2 lease(s) créé(s)', sortie.getvalue())

    def test_schedule_q2_pointe_vers_la_tache_par_regle(self):
        schedule = Schedule.objects.get(
            name=f'regle_generation_lease_{self.regle.id}'
        )

        self.assertEqual(
            schedule.func,
            'core.tasks.generer_leases_task',
        )

    def test_modifier_le_nom_ne_reinitialise_pas_next_run(self):
        nom_tache = f'regle_generation_lease_{self.regle.id}'
        next_run_attendu = occurrence_aware(2026, 8, 1, 12)
        Schedule.objects.filter(name=nom_tache).update(
            next_run=next_run_attendu,
        )

        self.regle.nom = 'Nouveau nom descriptif'
        self.regle.save(update_fields=['nom'])

        self.assertEqual(
            Schedule.objects.get(name=nom_tache).next_run,
            next_run_attendu,
        )

    def test_action_admin_attribue_la_regle_sans_deplacer_le_curseur(self):
        nouvelle_regle = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Nouvelle règle groupée',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 8, 1, 8),
            actif=True,
        )
        prochaine_echeance_initiale = self.contrat.prochaine_echeance
        request = RequestFactory().post(
            '/admin/recouvrement/contrat/',
            {
                'action': 'assigner_regle_generation',
                ACTION_CHECKBOX_NAME: [self.contrat.id],
                'regle_generation': nouvelle_regle.id,
                'appliquer': '1',
            },
        )
        contrat_admin = admin.site._registry[Contrat]

        with patch.object(contrat_admin, 'message_user') as message_user:
            resultat = contrat_admin.assigner_regle_generation(
                request,
                Contrat.objects.filter(pk=self.contrat.pk),
            )

        self.assertIsNone(resultat)
        message_user.assert_called_once()
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.regle_generation_id,
            nouvelle_regle.id,
        )
        self.assertEqual(
            self.contrat.prochaine_echeance,
            prochaine_echeance_initiale,
        )

    def test_action_admin_refuse_un_contrat_non_actif(self):
        nouvelle_regle = RegleGenerationLease.objects.create(
            compte_id=self.compte_id,
            nom='Règle refus contrat suspendu',
            frequence=Schedule.DAILY,
            debut=occurrence_aware(2026, 8, 1, 8),
            actif=True,
        )
        self.contrat.statut = Contrat.STATUT_SUSPENDU
        self.contrat.save(update_fields=['statut'])
        request = RequestFactory().post(
            '/admin/recouvrement/contrat/',
            {
                'action': 'assigner_regle_generation',
                ACTION_CHECKBOX_NAME: [self.contrat.id],
                'regle_generation': nouvelle_regle.id,
                'appliquer': '1',
            },
        )
        contrat_admin = admin.site._registry[Contrat]

        with patch.object(contrat_admin, 'message_user') as message_user:
            resultat = contrat_admin.assigner_regle_generation(
                request,
                Contrat.objects.filter(pk=self.contrat.pk),
            )

        self.assertIsNone(resultat)
        message_user.assert_called_once()
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.regle_generation_id,
            self.regle.id,
        )

    def test_regle_once_met_fin_au_curseur(self):
        self.regle.frequence = Schedule.ONCE
        self.regle.cron_expression = None
        self.regle.save()

        schedule = Schedule.objects.get(
            name=f'regle_generation_lease_{self.regle.id}'
        )
        self.assertEqual(schedule.repeats, 1)
        self.assertIsNone(schedule.cron)

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 7, 29, 12),
        )

        self.assertEqual(resultat['leases_crees'], 1)
        self.contrat.refresh_from_db()
        self.assertIsNone(self.contrat.prochaine_echeance)

    def test_session_mobile_assure_le_lease_suivant_du_dernier_paye(self):
        self.contrat.prochaine_echeance = occurrence_aware(2026, 7, 30, 12)
        self.contrat.save(update_fields=['prochaine_echeance'])

        leases_sources = [
            Lease.objects.create(
                compte_id=self.compte_id,
                contrat=self.contrat,
                date_echeance=occurrence,
                montant_attendu=Decimal('5000.00'),
            )
            for occurrence in [
                occurrence_aware(2026, 7, 29, 12),
                occurrence_aware(2026, 7, 29, 22),
            ]
        ]
        session = SessionPaiement.objects.create(
            compte_id=self.compte_id,
            reference='MOB.TEST.MULTI',
            config_paiement=self.config_paiement,
            montant_total=Decimal('10000.00'),
            utilisateur=self.chauffeur,
            statut=SessionPaiement.STATUT_VALIDE,
            date_validation=occurrence_aware(2026, 7, 29, 23),
        )
        for lease in leases_sources:
            Paiement.objects.create(
                compte_id=self.compte_id,
                contrat=self.contrat,
                lease=lease,
                enregistre_par=self.chauffeur,
                session=session,
                montant=Decimal('5000.00'),
                methode=Paiement.METHODE_MOBILE_MONEY,
            )

        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(session.id, 'SUCCESS')

        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .order_by('date_echeance')
                .values_list('date_echeance', flat=True)
            ),
            [
                occurrence_aware(2026, 7, 29, 12),
                occurrence_aware(2026, 7, 29, 22),
                occurrence_aware(2026, 7, 30, 12),
            ],
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 7, 30, 22),
        )

        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(session.id, 'SUCCESS')
        self.assertEqual(
            Lease.objects.filter(contrat=self.contrat).count(),
            3,
        )

    def test_paiement_utilise_le_curseur_si_le_lease_source_est_desaligne(self):
        self.regle.cron_expression = '0 8 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 6, 8)
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 5, 7),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        resultat = assurer_lease_suivant_du_lease(lease_source.id)

        self.assertEqual(resultat['statut'], 'CREE')
        self.assertFalse(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 5, 8),
            ).exists()
        )
        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 6, 8),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 7, 8),
        )

    def test_paiement_normalise_l_heure_incorrecte_du_curseur(self):
        self.regle.cron_expression = '30 9 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(
            2026, 8, 2, 9, 29
        )
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 1, 9, 30),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        resultat = assurer_lease_suivant_du_lease(lease_source.id)

        self.assertEqual(resultat['statut'], 'CREE')
        self.assertFalse(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 9, 29),
            ).exists()
        )
        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 9, 30),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 9, 30),
        )

    def test_paiement_prend_le_premier_creneau_officiel_manquant(self):
        self.regle.cron_expression = '0 10,23 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 2, 20)
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 1, 23),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        resultat = assurer_lease_suivant_du_lease(lease_source.id)

        self.assertEqual(resultat['statut'], 'CREE')
        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 10),
            ).exists()
        )
        self.assertFalse(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 20),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 2, 23),
        )

    def test_paiement_passe_au_second_creneau_si_le_premier_est_paye(self):
        self.regle.cron_expression = '0 10,23 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 2, 20)
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 2, 10),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        resultat = assurer_lease_suivant_du_lease(lease_source.id)

        self.assertEqual(resultat['statut'], 'CREE')
        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 23),
            ).exists()
        )
        self.assertFalse(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 20),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 10),
        )

    def test_paiement_saute_le_jour_de_repos_depuis_le_curseur(self):
        self.regle.cron_expression = '0 8 * * *'
        self.regle.save(update_fields=['cron_expression'])
        Parametre.objects.create(
            compte_id=self.compte_id,
            jours_repos=[0],
        )
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 3, 8)
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 2, 8),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        assurer_lease_suivant_du_lease(lease_source.id)

        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 4, 8),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 5, 8),
        )

    def test_paiement_autorise_deux_occurrences_le_meme_jour(self):
        self.contrat.prochaine_echeance = occurrence_aware(2026, 7, 29, 22)
        self.contrat.save(update_fields=['prochaine_echeance'])
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 7, 29, 12),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )

        assurer_lease_suivant_du_lease(lease_source.id)

        self.assertTrue(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 7, 29, 22),
            ).exists()
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 7, 30, 12),
        )

    def test_generation_daily_normalise_minuit_sur_heure_de_la_regle(self):
        self.regle.frequence = Schedule.DAILY
        self.regle.cron_expression = None
        self.regle.debut = occurrence_aware(2026, 7, 29, 2)
        self.regle.save()
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 2, 0)
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 2),
        )

        self.assertEqual(resultat['leases_crees'], 1)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .values_list('date_echeance', flat=True)
            ),
            [occurrence_aware(2026, 8, 2, 2)],
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 2),
        )

    def test_generation_cron_ne_cree_pas_le_lease_du_curseur_desaligne(self):
        self.regle.cron_expression = '30 9 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(
            2026, 8, 2, 9, 29
        )
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 9, 30),
        )

        self.assertEqual(resultat['leases_crees'], 1)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .values_list('date_echeance', flat=True)
            ),
            [occurrence_aware(2026, 8, 2, 9, 30)],
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 9, 30),
        )

    def test_generation_cron_interprete_le_debut_dans_le_fuseau_local(self):
        self.regle.cron_expression = '31 12 * * *'
        self.regle.debut = occurrence_aware(2026, 8, 2, 8)
        self.regle.save(update_fields=['cron_expression', 'debut'])
        self.contrat.prochaine_echeance = occurrence_aware(
            2026, 8, 2, 12, 30
        )
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 12, 31),
        )

        self.assertEqual(resultat['leases_crees'], 1)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .values_list('date_echeance', flat=True)
            ),
            [occurrence_aware(2026, 8, 2, 12, 31)],
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 12, 31),
        )

    def test_generation_ignore_un_curseur_superieur_a_la_limite(self):
        self.regle.cron_expression = '30 9 * * *'
        self.regle.save(update_fields=['cron_expression'])
        curseur = occurrence_aware(2026, 8, 2, 9, 31)
        self.contrat.prochaine_echeance = curseur
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 9, 30),
        )

        self.assertEqual(resultat['contrats_cibles'], 0)
        self.assertEqual(resultat['leases_crees'], 0)
        self.assertFalse(Lease.objects.filter(contrat=self.contrat).exists())
        self.contrat.refresh_from_db()
        self.assertEqual(self.contrat.prochaine_echeance, curseur)

    def test_generation_rattrape_un_curseur_superieur_au_passage_precedent(self):
        self.regle.cron_expression = '30 9 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(
            2026, 8, 2, 9, 31
        )
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 3, 9, 30),
        )

        self.assertEqual(resultat['leases_crees'], 2)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .order_by('date_echeance')
                .values_list('date_echeance', flat=True)
            ),
            [
                occurrence_aware(2026, 8, 2, 9, 30),
                occurrence_aware(2026, 8, 3, 9, 30),
            ],
        )
        self.assertFalse(
            Lease.objects.filter(
                contrat=self.contrat,
                date_echeance=occurrence_aware(2026, 8, 2, 9, 31),
            ).exists()
        )

    def test_generation_rattrape_les_creneaux_officiels_manquants(self):
        self.regle.cron_expression = '0 10,23 * * *'
        self.regle.save(update_fields=['cron_expression'])
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 2, 20)
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 23),
        )

        self.assertEqual(resultat['leases_crees'], 2)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .order_by('date_echeance')
                .values_list('date_echeance', flat=True)
            ),
            [
                occurrence_aware(2026, 8, 2, 10),
                occurrence_aware(2026, 8, 2, 23),
            ],
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 8, 3, 10),
        )

    def test_generation_ne_duplique_pas_un_creneau_officiel_existant(self):
        self.regle.cron_expression = '0 10,23 * * *'
        self.regle.save(update_fields=['cron_expression'])
        Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 8, 2, 10),
            montant_attendu=Decimal('5000.00'),
        )
        self.contrat.prochaine_echeance = occurrence_aware(2026, 8, 2, 20)
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = generer_leases_pour_regle(
            self.regle.id,
            occurrence_aware(2026, 8, 2, 23),
        )

        self.assertEqual(resultat['leases_crees'], 1)
        self.assertEqual(resultat['doublons_ignores'], 1)
        self.assertEqual(
            list(
                Lease.objects.filter(contrat=self.contrat)
                .order_by('date_echeance')
                .values_list('date_echeance', flat=True)
            ),
            [
                occurrence_aware(2026, 8, 2, 10),
                occurrence_aware(2026, 8, 2, 23),
            ],
        )

    def test_paiement_ne_genere_pas_si_un_lease_posterieur_existe(self):
        lease_source = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 7, 29, 12),
            montant_attendu=Decimal('5000.00'),
            montant_paye=Decimal('5000.00'),
            statut=Lease.STATUT_PAYE,
        )
        lease_suivant = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 7, 29, 22),
            montant_attendu=Decimal('5000.00'),
        )
        self.contrat.prochaine_echeance = occurrence_aware(2026, 7, 30, 12)
        self.contrat.save(update_fields=['prochaine_echeance'])

        resultat = assurer_lease_suivant_du_lease(lease_source.id)

        self.assertEqual(resultat['statut'], 'EXISTANT')
        self.assertEqual(resultat['lease_id'], lease_suivant.id)
        self.assertEqual(
            Lease.objects.filter(contrat=self.contrat).count(),
            2,
        )
        self.contrat.refresh_from_db()
        self.assertEqual(
            self.contrat.prochaine_echeance,
            occurrence_aware(2026, 7, 30, 12),
        )

    def test_paiement_mobile_partiel_ne_declenche_pas_de_generation(self):
        lease = Lease.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            date_echeance=occurrence_aware(2026, 7, 29, 12),
            montant_attendu=Decimal('5000.00'),
        )
        session = SessionPaiement.objects.create(
            compte_id=self.compte_id,
            reference='MOB.TEST.PARTIEL',
            config_paiement=self.config_paiement,
            montant_total=Decimal('2000.00'),
            utilisateur=self.chauffeur,
            statut=SessionPaiement.STATUT_VALIDE,
            date_validation=occurrence_aware(2026, 7, 29, 13),
        )
        Paiement.objects.create(
            compte_id=self.compte_id,
            contrat=self.contrat,
            lease=lease,
            enregistre_par=self.chauffeur,
            session=session,
            montant=Decimal('2000.00'),
            methode=Paiement.METHODE_MOBILE_MONEY,
        )

        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(session.id, 'SUCCESS')

        lease.refresh_from_db()
        self.assertEqual(lease.statut, Lease.STATUT_PARTIEL)
        self.assertEqual(
            Lease.objects.filter(contrat=self.contrat).count(),
            1,
        )
