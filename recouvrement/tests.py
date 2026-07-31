from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django_q.models import Schedule

from accounts.models import CustomUser
from core.tasks import (
    generer_leases_task,
    paiement_task,
)
from recouvrement.admin import (
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
from recouvrement.services import (
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
