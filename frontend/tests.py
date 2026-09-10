from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser, CustomUserRole, Role
from core.tasks import paiement_task
from recouvrement.models import (
    Agence, CompteReceptionProprietaire, Contrat, Lease, Paiement,
    PreuvePaiementUSSD, Proprietaire, SessionPaiement, TypeContrat,
)


def _permissions(*codenames):
    return list(Permission.objects.filter(codename__in=codenames))


class FrontendTestCase(TestCase):
    """Base commune : deux comptes partenaires isolés + un rôle 'complet'."""

    @classmethod
    def setUpTestData(cls):
        cls.role_partner_admin = Role.objects.create(
            libelle="Partner Admin Test", slug="TEST_PARTNER_ADMIN",
        )
        cls.role_partner_admin.permissions.set(_permissions(
            'view_contrat', 'add_contrat', 'change_contrat', 'delete_contrat',
            'view_all_contrats',
            'view_lease', 'view_all_leases',
            'view_paiement', 'add_paiement', 'change_paiement', 'view_all_paiements',
            'view_customuser', 'add_customuser', 'change_customuser', 'delete_customuser',
            'view_sessionpaiement', 'view_all_sessionpaiements',
            'view_preuvepaiementussd', 'can_validate_ussd_payment',
            'view_proprietaire', 'add_proprietaire', 'change_proprietaire',
            'view_comptereceptionproprietaire', 'add_comptereceptionproprietaire',
            'change_comptereceptionproprietaire',
            'view_typecontrat', 'add_typecontrat', 'change_typecontrat',
        ))

        # Rôle "vérificateur USSD partiel" : peut voir les preuves mais pas
        # les valider/rejeter (permission can_validate_ussd_payment absente)
        # -> sert à vérifier que la double exigence de permission est respectée.
        cls.role_verificateur_partiel = Role.objects.create(
            libelle="Verificateur Partiel Test", slug="TEST_VERIF_PARTIEL",
        )
        cls.role_verificateur_partiel.permissions.set(_permissions(
            'view_sessionpaiement', 'view_all_sessionpaiements', 'view_preuvepaiementussd',
        ))

        cls.compte_a = 900101
        cls.compte_b = 900102

        cls.admin_a = CustomUser.objects.create(
            keycloak_id="frontend-admin-a", compte_id=cls.compte_a,
            email="admin-a@example.com", nom_complet="Admin A",
        )
        CustomUserRole.objects.create(
            compte_id=cls.compte_a, user=cls.admin_a, role=cls.role_partner_admin, actif=True,
        )

        cls.admin_b = CustomUser.objects.create(
            keycloak_id="frontend-admin-b", compte_id=cls.compte_b,
            email="admin-b@example.com", nom_complet="Admin B",
        )
        CustomUserRole.objects.create(
            compte_id=cls.compte_b, user=cls.admin_b, role=cls.role_partner_admin, actif=True,
        )

        cls.sans_droits = CustomUser.objects.create(
            keycloak_id="frontend-sans-droits", compte_id=cls.compte_a,
            email="sans-droits@example.com", nom_complet="Sans Droits",
        )

        cls.verificateur_partiel = CustomUser.objects.create(
            keycloak_id="frontend-verif-partiel", compte_id=cls.compte_a,
            email="verif-partiel@example.com", nom_complet="Verif Partiel",
        )
        CustomUserRole.objects.create(
            compte_id=cls.compte_a, user=cls.verificateur_partiel,
            role=cls.role_verificateur_partiel, actif=True,
        )

        cls.chauffeur_a = CustomUser.objects.create(
            keycloak_id="frontend-chauffeur-a", compte_id=cls.compte_a,
            email="chauffeur-a@example.com", nom_complet="Chauffeur A",
        )
        # Second chauffeur, sans contrat actif : nécessaire pour tester une
        # création, sinon la règle "un seul contrat actif par chauffeur"
        # (déjà respectée par chauffeur_a via contrat_a ci-dessous) bloque.
        cls.chauffeur_a2 = CustomUser.objects.create(
            keycloak_id="frontend-chauffeur-a2", compte_id=cls.compte_a,
            email="chauffeur-a2@example.com", nom_complet="Chauffeur A2",
        )
        # Slug exact 'DRIVER' requis : GestionChauffeurSerializer.create() et
        # _chauffeurs_accessibles() cherchent tous deux ce slug littéral. La
        # base de test est toujours fraîche (migrations seules), donc aucune
        # collision avec un rôle 'DRIVER' de données de seed réelles.
        role_driver = Role.objects.create(libelle="Driver Test", slug="DRIVER")
        for chauffeur in (cls.chauffeur_a, cls.chauffeur_a2):
            CustomUserRole.objects.create(
                compte_id=cls.compte_a, user=chauffeur, role=role_driver, actif=True,
            )

        cls.type_contrat_a = TypeContrat.objects.create(
            compte_id=cls.compte_a, libelle="Moto Test", code="MOTOTESTFE", est_principal=True,
        )
        cls.type_accessoire_a = TypeContrat.objects.create(
            compte_id=cls.compte_a, libelle="Téléphone Test", code="TELTESTFE", est_principal=False,
        )
        cls.proprietaire_a = Proprietaire.objects.create(
            compte_id=cls.compte_a, nom_complet="Proprietaire Test A", actif=True,
        )
        cls.agence_a = Agence.objects.create(
            compte_id=cls.compte_a, nom="Agence Test A", code="AGTESTFE",
        )

        cls.contrat_a = Contrat.objects.create(
            compte_id=cls.compte_a, chauffeur=cls.chauffeur_a, type_contrat=cls.type_contrat_a,
            agence=cls.agence_a, proprietaire=cls.proprietaire_a,
            nom_complet=cls.chauffeur_a.nom_complet,
            immatriculation="FE-001-TT", vin="VINFRONTENDTEST01",
            montant_total=Decimal("100000.00"), montant_restant=Decimal("100000.00"),
            montant_par_paiement=Decimal("5000.00"),
            date_debut=date(2026, 1, 1), date_fin=date(2027, 1, 1),
        )
        cls.lease_a = Lease.objects.create(
            compte_id=cls.compte_a, agence=cls.agence_a, contrat=cls.contrat_a,
            date_echeance="2026-09-09T08:00:00+01:00",
            montant_attendu=Decimal("5000.00"),
        )

        cls.compte_reception_a = CompteReceptionProprietaire.objects.create(
            compte_id=cls.compte_a, proprietaire=cls.proprietaire_a,
            operateur="ORANGE", numero="690000001", nom_titulaire="Proprietaire Test A",
            actif=True,
        )

        # Session + preuve USSD en EN_VERIFICATION, pour tester valider/rejeter
        # depuis l'écran Transactions sans repasser par tout le flux de paiement.
        cls.session_ussd_a = SessionPaiement.objects.create(
            compte_id=cls.compte_a, agence=cls.agence_a,
            reference="USSD.FRONTENDTEST.0001", canal=SessionPaiement.CANAL_USSD_ASSISTE,
            montant_total=Decimal("5000.00"), telephone="237677000001",
            utilisateur=cls.chauffeur_a, proprietaire=cls.proprietaire_a,
            compte_reception=cls.compte_reception_a, operateur="ORANGE",
            numero_destinataire=cls.compte_reception_a.numero,
            nom_destinataire=cls.compte_reception_a.nom_titulaire,
            statut=SessionPaiement.STATUT_EN_VERIFICATION,
        )
        cls.paiement_ussd_a = Paiement.objects.create(
            compte_id=cls.compte_a, agence=cls.agence_a, contrat=cls.contrat_a,
            lease=cls.lease_a, session=cls.session_ussd_a, enregistre_par=cls.chauffeur_a,
            montant=Decimal("5000.00"), methode=Paiement.METHODE_USSD_ASSISTE,
            statut=Paiement.STATUT_EN_ATTENTE,
        )
        cls.preuve_ussd_a = PreuvePaiementUSSD.objects.create(
            compte_id=cls.compte_a, session=cls.session_ussd_a, operateur="ORANGE",
            reference_operateur="OMFRONTENDTEST0001", texte_brut="Transfert de 5000 F...",
            montant_transfere=Decimal("5000.00"), numero_expediteur="237677000001",
            numero_destinataire=cls.compte_reception_a.numero,
            capture_appareil_le="2026-09-09T08:05:00+01:00",
            statut=PreuvePaiementUSSD.STATUT_EN_VERIFICATION,
        )

    # Pas de tearDownClass manuel : django.test.TestCase encapsule déjà
    # setUpTestData() + chaque test dans des transactions annulées
    # automatiquement. Un nettoyage manuel ici supprimerait les Contrat
    # avant leurs Lease (FK PROTECT), lèverait ProtectedError, et
    # empêcherait le rollback automatique de s'exécuter.

class AccesAnonymeTests(FrontendTestCase):
    def test_liste_contrats_redirige_vers_login(self):
        response = self.client.get(reverse('frontend:contrats-liste'))
        # fetch_redirect_response=False : la page de login elle-même
        # redirige ensuite vers Keycloak (302, pas 200), donc on ne vérifie
        # que le premier saut ici, pas toute la chaîne.
        self.assertRedirects(
            response,
            f"{reverse('frontend:login')}?next={reverse('frontend:contrats-liste')}",
            fetch_redirect_response=False,
        )


class IsolationTenantTests(FrontendTestCase):
    def test_contrat_dun_autre_compte_est_introuvable(self):
        self.client.force_login(self.admin_b)
        response = self.client.get(
            reverse('frontend:contrats-detail', args=[self.contrat_a.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_liste_contrats_ne_montre_que_son_propre_compte(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(reverse('frontend:contrats-liste'))
        self.assertContains(response, self.contrat_a.reference)


class PermissionTests(FrontendTestCase):
    def test_creation_sans_permission_est_refusee(self):
        self.client.force_login(self.sans_droits)
        response = self.client.get(reverse('frontend:contrats-creer'))
        self.assertEqual(response.status_code, 403)

    def test_liste_sans_permission_est_refusee(self):
        self.client.force_login(self.sans_droits)
        response = self.client.get(reverse('frontend:paiements-liste'))
        self.assertEqual(response.status_code, 403)


class CycleContratTests(FrontendTestCase):
    def test_creer_contrat_ajouter_sous_contrat_et_encaisser(self):
        self.client.force_login(self.admin_a)

        # 1. Création du contrat principal
        response = self.client.post(reverse('frontend:contrats-creer'), {
            'chauffeur': self.chauffeur_a2.pk,
            'type_contrat': self.type_contrat_a.pk,
            'proprietaire': self.proprietaire_a.pk,
            'agence': self.agence_a.pk,
            'immatriculation': 'FE-002-TT',
            'vin': 'VINFRONTENDTEST02',
            'montant_total': '90000.00',
            'montant_par_paiement': '3000.00',
            'montant_paye': '0',
            'frequence': 'JOURNALIER',
            'date_debut': '2026-01-01',
            'date_fin': '2027-01-01',
            'prochaine_echeance': '2026-01-02T08:00',
        })
        contrat = Contrat.objects.get(vin='VINFRONTENDTEST02')
        self.assertRedirects(
            response, reverse('frontend:contrats-detail', args=[contrat.pk])
        )
        self.assertEqual(contrat.compte_id, self.compte_a)
        self.assertEqual(contrat.enregistre_par_id, self.admin_a.pk)

        # 2. Ajout d'un sous-contrat, hérite du parent
        response = self.client.post(
            reverse('frontend:contrats-sous-contrat', args=[contrat.pk]),
            {
                'type_contrat': self.type_accessoire_a.pk,
                'montant_total': '20000.00',
                'montant_par_paiement': '1000.00',
                'montant_paye': '0',
                'frequence': 'JOURNALIER',
                'date_debut': '2026-01-01',
                'date_fin': '2027-01-01',
                'prochaine_echeance': '2026-01-02T08:00',
            },
        )
        self.assertRedirects(
            response, reverse('frontend:contrats-detail', args=[contrat.pk])
        )
        sous_contrat = contrat.sous_contrats.get()
        self.assertEqual(sous_contrat.proprietaire_id, self.proprietaire_a.pk)
        self.assertEqual(sous_contrat.agence_id, self.agence_a.pk)
        self.assertEqual(sous_contrat.chauffeur_id, self.chauffeur_a2.pk)

        # 3. Encaissement en espèces sur l'échéance existante du contrat de test
        response = self.client.post(reverse('frontend:paiements-creer'), {
            'lease_id': self.lease_a.pk,
            'montant': '2000.00',
        })
        self.assertRedirects(
            response, reverse('frontend:contrats-detail', args=[self.contrat_a.pk])
        )
        self.lease_a.refresh_from_db()
        self.contrat_a.refresh_from_db()
        self.assertEqual(self.lease_a.montant_paye, Decimal('2000.00'))
        self.assertEqual(self.lease_a.statut, Lease.STATUT_PARTIEL)
        self.assertEqual(self.contrat_a.montant_paye, Decimal('2000.00'))
        self.assertEqual(self.contrat_a.montant_restant, Decimal('98000.00'))


class LoginCallbackTests(FrontendTestCase):
    """
    Le round-trip Keycloak réel n'est pas testable en automatisé : on mocke
    l'échange de code et la validation du token, pour ne tester que notre
    propre code (session, provisioning, redirection).
    """

    def test_callback_sans_state_redirige_avec_erreur(self):
        response = self.client.get(reverse('frontend:login-callback'), {'code': 'abc'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('erreur=', response.url)

    @patch('frontend.views.auth.provisionner_utilisateur_depuis_token')
    @patch('frontend.views.auth.KeycloakJWTAuthentication.get_validated_token')
    @patch('frontend.views.auth.KeycloakOpenID.token')
    def test_callback_valide_connecte_lutilisateur(
        self, mock_token, mock_get_validated_token, mock_provisionner,
    ):
        session = self.client.session
        session['keycloak_state'] = 'etat-test'
        session['keycloak_next'] = ''
        session.save()

        mock_token.return_value = {'access_token': 'jeton-factice'}
        mock_get_validated_token.return_value = {'sub': 'kc-id'}
        mock_provisionner.return_value = self.admin_a

        response = self.client.get(reverse('frontend:login-callback'), {
            'code': 'code-factice', 'state': 'etat-test',
        })
        self.assertRedirects(response, reverse('frontend:contrats-liste'))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin_a.pk)


class ChauffeurTests(FrontendTestCase):
    def test_creation_assigne_role_driver_et_mot_de_passe_inutilisable(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(reverse('frontend:chauffeurs-creer'), {
            'keycloak_id': 'kc-nouveau-chauffeur',
            'email': 'nouveau@example.com',
            'nom_complet': 'Nouveau Chauffeur',
        })
        chauffeur = CustomUser.objects.get(keycloak_id='kc-nouveau-chauffeur')
        self.assertRedirects(
            response, reverse('frontend:chauffeurs-detail', args=[chauffeur.pk])
        )
        self.assertEqual(chauffeur.compte_id, self.compte_a)
        self.assertFalse(chauffeur.has_usable_password())
        self.assertTrue(
            CustomUserRole.objects.filter(user=chauffeur, role__slug='DRIVER').exists()
        )

    def test_suppression_sans_historique_est_definitive(self):
        self.client.force_login(self.admin_a)
        self.assertFalse(self.chauffeur_a2.contrats_a_payer.exists())
        response = self.client.post(
            reverse('frontend:chauffeurs-supprimer', args=[self.chauffeur_a2.pk])
        )
        self.assertRedirects(response, reverse('frontend:chauffeurs-liste'))
        self.assertFalse(CustomUser.objects.filter(pk=self.chauffeur_a2.pk).exists())

    def test_suppression_avec_historique_desactive_seulement(self):
        self.client.force_login(self.admin_a)
        self.assertTrue(self.chauffeur_a.contrats_a_payer.exists())
        response = self.client.post(
            reverse('frontend:chauffeurs-supprimer', args=[self.chauffeur_a.pk])
        )
        self.assertRedirects(
            response, reverse('frontend:chauffeurs-detail', args=[self.chauffeur_a.pk])
        )
        self.chauffeur_a.refresh_from_db()
        self.assertFalse(self.chauffeur_a.is_active)

    def test_isolation_tenant_sur_le_detail(self):
        self.client.force_login(self.admin_b)
        response = self.client.get(
            reverse('frontend:chauffeurs-detail', args=[self.chauffeur_a.pk])
        )
        self.assertEqual(response.status_code, 404)


class TransactionTests(FrontendTestCase):
    def test_liste_isolee_par_tenant(self):
        self.client.force_login(self.admin_b)
        response = self.client.get(reverse('frontend:transactions-liste'))
        self.assertNotContains(response, self.session_ussd_a.reference)

    def test_detail_affiche_la_preuve_ussd(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(
            reverse('frontend:transactions-detail', args=[self.session_ussd_a.pk])
        )
        self.assertContains(response, self.preuve_ussd_a.reference_operateur)

    def test_valider_preuve_sans_permission_dediee_est_refuse(self):
        self.client.force_login(self.verificateur_partiel)
        response = self.client.post(
            reverse('frontend:transactions-valider-ussd', args=[self.session_ussd_a.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.preuve_ussd_a.refresh_from_db()
        self.assertEqual(self.preuve_ussd_a.statut, PreuvePaiementUSSD.STATUT_EN_VERIFICATION)

    def test_valider_preuve_solde_le_lease(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:transactions-valider-ussd', args=[self.session_ussd_a.pk])
        )
        self.assertRedirects(
            response, reverse('frontend:transactions-detail', args=[self.session_ussd_a.pk])
        )
        self.preuve_ussd_a.refresh_from_db()
        self.assertEqual(self.preuve_ussd_a.statut, PreuvePaiementUSSD.STATUT_VALIDEE)

        # La ventilation réelle (solde du lease/contrat) est planifiée via
        # transaction.on_commit(), qui ne se déclenche jamais dans une
        # transaction de test (jamais commitée) — on simule ici le worker
        # Q2 en rejouant paiement_task nous-mêmes, comme le fait déjà
        # recouvrement/tests.py pour ce même service.
        with patch('core.tasks.notifier_utilisateur'):
            paiement_task(self.session_ussd_a.id, 'SUCCESS')

        self.lease_a.refresh_from_db()
        self.assertEqual(self.lease_a.statut, Lease.STATUT_PAYE)

    def test_rejeter_preuve_sans_motif_est_refuse(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:transactions-rejeter-ussd', args=[self.session_ussd_a.pk]),
            {'motif': ''},
        )
        self.assertRedirects(
            response, reverse('frontend:transactions-detail', args=[self.session_ussd_a.pk])
        )
        self.preuve_ussd_a.refresh_from_db()
        self.assertEqual(self.preuve_ussd_a.statut, PreuvePaiementUSSD.STATUT_EN_VERIFICATION)

    def test_rejeter_preuve_avec_motif(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:transactions-rejeter-ussd', args=[self.session_ussd_a.pk]),
            {'motif': 'Référence illisible'},
        )
        self.assertRedirects(
            response, reverse('frontend:transactions-detail', args=[self.session_ussd_a.pk])
        )
        self.preuve_ussd_a.refresh_from_db()
        self.assertEqual(self.preuve_ussd_a.statut, PreuvePaiementUSSD.STATUT_REJETEE)


class ProprietaireTests(FrontendTestCase):
    def test_creer_proprietaire(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(reverse('frontend:proprietaires-creer'), {
            'nom_complet': '  Nouveau Proprietaire  ',
        })
        proprietaire = Proprietaire.objects.get(nom_complet='Nouveau Proprietaire')
        self.assertRedirects(
            response, reverse('frontend:proprietaires-detail', args=[proprietaire.pk])
        )
        self.assertEqual(proprietaire.compte_id, self.compte_a)

    def test_ajouter_compte_reception_normalise_operateur_et_numero(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:proprietaires-compte-reception-creer', args=[self.proprietaire_a.pk]),
            {'operateur': 'mtn', 'numero': '690169694', 'nom_titulaire': 'MBY Christian'},
        )
        self.assertRedirects(
            response, reverse('frontend:proprietaires-detail', args=[self.proprietaire_a.pk])
        )
        compte = self.proprietaire_a.comptes_reception.get(operateur='MTN')
        self.assertEqual(compte.numero, '237690169694')

    def test_ajouter_compte_reception_numero_invalide_est_refuse(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:proprietaires-compte-reception-creer', args=[self.proprietaire_a.pk]),
            {'operateur': 'MTN', 'numero': 'abc', 'nom_titulaire': 'Test'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            self.proprietaire_a.comptes_reception.filter(operateur='MTN').exists()
        )

    def test_basculer_compte_reception(self):
        self.client.force_login(self.admin_a)
        self.assertTrue(self.compte_reception_a.actif)
        response = self.client.post(reverse(
            'frontend:proprietaires-compte-reception-basculer',
            args=[self.proprietaire_a.pk, self.compte_reception_a.pk],
        ))
        self.assertRedirects(
            response, reverse('frontend:proprietaires-detail', args=[self.proprietaire_a.pk])
        )
        self.compte_reception_a.refresh_from_db()
        self.assertFalse(self.compte_reception_a.actif)

    def test_isolation_tenant_sur_le_detail(self):
        self.client.force_login(self.admin_b)
        response = self.client.get(
            reverse('frontend:proprietaires-detail', args=[self.proprietaire_a.pk])
        )
        self.assertEqual(response.status_code, 404)


class TypeContratTests(FrontendTestCase):
    def test_creer_type_contrat(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(reverse('frontend:types-contrat-creer'), {
            'libelle': 'Groupe électrogène',
            'code': 'GROUPE-ELEC',
            'est_principal': 'true',
        })
        self.assertRedirects(response, reverse('frontend:types-contrat-liste'))
        type_contrat = TypeContrat.objects.get(code='GROUPE-ELEC')
        self.assertEqual(type_contrat.compte_id, self.compte_a)
        self.assertTrue(type_contrat.est_principal)

    def test_modifier_decoche_est_principal(self):
        self.client.force_login(self.admin_a)
        self.assertTrue(self.type_contrat_a.est_principal)
        response = self.client.post(
            reverse('frontend:types-contrat-modifier', args=[self.type_contrat_a.pk]),
            {
                'libelle': self.type_contrat_a.libelle,
                'code': self.type_contrat_a.code,
                'est_principal': 'false',
            },
        )
        self.assertRedirects(response, reverse('frontend:types-contrat-liste'))
        self.type_contrat_a.refresh_from_db()
        self.assertFalse(self.type_contrat_a.est_principal)

    def test_code_deja_utilise_dans_le_compte_est_refuse(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(reverse('frontend:types-contrat-creer'), {
            'libelle': 'Doublon',
            'code': self.type_contrat_a.code,
            'est_principal': 'false',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TypeContrat.objects.filter(libelle='Doublon').exists())

    def test_isolation_tenant_sur_la_liste(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(reverse('frontend:types-contrat-liste'))
        self.assertContains(response, self.type_contrat_a.libelle)

        self.client.force_login(self.admin_b)
        response = self.client.get(reverse('frontend:types-contrat-liste'))
        self.assertNotContains(response, self.type_contrat_a.libelle)


class SpecificitesContratTests(FrontendTestCase):
    def test_creation_avec_specificites(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(reverse('frontend:contrats-creer'), {
            'chauffeur': self.chauffeur_a2.pk,
            'type_contrat': self.type_contrat_a.pk,
            'proprietaire': self.proprietaire_a.pk,
            'agence': self.agence_a.pk,
            'immatriculation': 'FE-003-TT',
            'vin': 'VINFRONTENDTEST03',
            'montant_total': '90000.00',
            'montant_par_paiement': '3000.00',
            'montant_paye': '0',
            'frequence': 'JOURNALIER',
            'date_debut': '2026-01-01',
            'date_fin': '2027-01-01',
            'prochaine_echeance': '2026-01-02T08:00',
            'specificite_cle': ['marque', 'couleur', ''],
            'specificite_valeur': ['Yamaha', 'Rouge', 'ignoree'],
        })
        contrat = Contrat.objects.get(vin='VINFRONTENDTEST03')
        self.assertRedirects(response, reverse('frontend:contrats-detail', args=[contrat.pk]))
        self.assertEqual(contrat.specificites, {'marque': 'Yamaha', 'couleur': 'Rouge'})

    def test_modification_remplace_les_specificites(self):
        self.contrat_a.specificites = {'ancienne_cle': 'ancienne_valeur'}
        self.contrat_a.save(update_fields=['specificites'])
        self.client.force_login(self.admin_a)

        response = self.client.get(reverse('frontend:contrats-modifier', args=[self.contrat_a.pk]))
        self.assertContains(response, 'ancienne_cle')

        response = self.client.post(
            reverse('frontend:contrats-modifier', args=[self.contrat_a.pk]),
            {
                'specificite_cle': ['nouvelle_cle'],
                'specificite_valeur': ['nouvelle_valeur'],
            },
        )
        self.assertRedirects(
            response, reverse('frontend:contrats-detail', args=[self.contrat_a.pk])
        )
        self.contrat_a.refresh_from_db()
        self.assertEqual(self.contrat_a.specificites, {'nouvelle_cle': 'nouvelle_valeur'})

    def test_ajout_sous_contrat_avec_specificites(self):
        self.client.force_login(self.admin_a)
        response = self.client.post(
            reverse('frontend:contrats-sous-contrat', args=[self.contrat_a.pk]),
            {
                'type_contrat': self.type_accessoire_a.pk,
                'montant_total': '20000.00',
                'montant_par_paiement': '1000.00',
                'montant_paye': '0',
                'frequence': 'JOURNALIER',
                'date_debut': '2026-01-01',
                'date_fin': '2027-01-01',
                'prochaine_echeance': '2026-01-02T08:00',
                'specificite_cle': ['imei'],
                'specificite_valeur': ['123456789012345'],
            },
        )
        self.assertRedirects(
            response, reverse('frontend:contrats-detail', args=[self.contrat_a.pk])
        )
        sous_contrat = self.contrat_a.sous_contrats.get()
        self.assertEqual(sous_contrat.specificites, {'imei': '123456789012345'})
