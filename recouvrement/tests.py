import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from django_q.models import Schedule
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import ConfigPaiement, CustomUser
from core.exceptions import CustomAPIException
from core.tasks import (
    generer_leases_task,
    paiement_task,
    verifier_statut_session_task,
)
from recouvrement.admin import (
    AssignerConfigPaiementContratsForm,
    AssignerRegleGenerationContratsForm,
    ContratAdminForm,
    RegleGenerationLeaseAdminForm,
)
from recouvrement.models import (
    Contrat,
    Lease,
    Paiement,
    RegleGenerationLease,
    SessionPaiement,
    TypeContrat,
)
from recouvrement.api.v1.views import (
    InitiationPaiementView,
    WebhookView,
)
from recouvrement.api.v1.serializers import (
    CalendrierSerializer,
    ContratSerializer,
    LeaseSerializer,
)
from recouvrement.services import (
    PaymentService,
    calculer_prochaine_occurrence,
    generer_leases_pour_regle,
)


def occurrence_aware(annee, mois, jour, heure):
    return timezone.make_aware(
        datetime(annee, mois, jour, heure),
        timezone.get_current_timezone(),
    )


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
        self.config_defaut = ConfigPaiement.objects.create(
            compte_id=self.compte_id,
            nom='Encaissement principal',
            api_key='api-key-principale',
            base_url='https://principal.paygate.test',
            success_url='https://principal.test/success',
            webhook_secret='secret-principal',
            actif=True,
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
        self.contrat_special = self._creer_contrat(
            nom='Sous-contrat configuration spéciale',
            parent=self.contrat_defaut,
            config_paiement=self.config_speciale,
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
    ):
        return Contrat.objects.create(
            compte_id=self.compte_id,
            chauffeur=self.utilisateur,
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
        self.assertEqual(
            traiter.call_args.kwargs['config_paiement_id'],
            self.config_speciale.id,
        )

    def test_initiation_mixte_retourne_400_sans_creer_de_session(self):
        request = APIRequestFactory().post(
            '/api/v1/initier-paiement/',
            {
                'lignes': [
                    {'lease_id': self.lease_defaut.id, 'montant': '10.00'},
                    {'lease_id': self.lease_special.id, 'montant': '10.00'},
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


class AdministrationGenerationLeaseTests(TestCase):
    def test_regle_generation_est_enregistree_dans_admin(self):
        self.assertTrue(
            admin.site.is_registered(RegleGenerationLease)
        )

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
