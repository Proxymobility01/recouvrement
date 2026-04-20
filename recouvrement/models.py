import random
import string
from decimal import Decimal
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import BaseModel, CustomUser


# Create your models here.
# ==========================================
# 4. LOGIQUE MÉTIER : CONTRATS
# ==========================================
class Contrat(BaseModel):
    # --- Constantes de Statut ---
    STATUT_ACTIF = 'ACTIF'
    STATUT_SUSPENDU = 'SUSPENDU'
    STATUT_SOLDE = 'SOLDE'
    STATUT_CONTENTIEUX = 'CONTENTIEUX'

    JOURNALIER = 'JOURNALIER'
    HEBDOMADAIRE = 'HEBDOMADAIRE'
    MENSUEL = 'MENSUEL'

    FREQUENCE_CHOICES = [
        (JOURNALIER, 'Journalier'),
        (HEBDOMADAIRE, 'Hebdomadaire'),
        (MENSUEL, 'Mensuel (Par mois)'),
    ]

    STATUT_CHOICES = [
        (STATUT_ACTIF, 'Actif'),
        (STATUT_SUSPENDU, 'Suspendu'),
        (STATUT_SOLDE, 'Soldé'),
        (STATUT_CONTENTIEUX, 'Contentieux'),
    ]

    chauffeur = models.ForeignKey(
        CustomUser,
        on_delete=models.PROTECT,
        related_name="contrats_a_payer",
        help_text="Le chauffeur (DRIVER) qui loue le véhicule et doit payer."
    )

    enregistre_par = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contrats_enregistres",
        help_text="L'agent (PARTNER_ADMIN) qui a saisi ce contrat dans le système."
    )

    immatriculation = models.CharField(max_length=20,null=True, blank=True)

    nom_complet = models.CharField(max_length=255)

    montant_total = models.DecimalField(max_digits=12, decimal_places=2)
    montant_restant = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    montant_par_paiement = models.DecimalField(max_digits=12, decimal_places=2)

    frequence = models.CharField(max_length=50,choices=FREQUENCE_CHOICES, default=JOURNALIER)
    date_debut = models.DateField()
    date_fin = models.DateField(null=True, blank=True)
    prochaine_echeance = models.DateField(db_index=True)

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_ACTIF)

    class Meta:
        db_table = "recouvrement_contrat"
        permissions = [
            ("view_all_contrats", "Peut voir tous les contrats de son entreprise"),
        ]


    def __str__(self):
        return f"Contrat {self.id} - {self.nom_complet}"


class Lease(BaseModel):
    # --- Constantes de Statut ---
    STATUT_NON_PAYE = 'NON_PAYE'
    STATUT_PARTIEL = 'PARTIEL'
    STATUT_PAYE = 'PAYE'

    STATUT_CHOICES = [
        (STATUT_NON_PAYE, 'Non payé'),
        (STATUT_PARTIEL, 'Partiellement payé'),
        (STATUT_PAYE, 'Payé'),
    ]

    contrat = models.ForeignKey(
        Contrat,
        on_delete=models.PROTECT,
        related_name="leases",
        help_text="Le contrat auquel cette échéance journalière est rattachée."
    )

    date_echeance = models.DateField(db_index=True)

    montant_attendu = models.DecimalField(max_digits=12, decimal_places=2)

    montant_paye = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_NON_PAYE)

    class Meta:
        db_table = "recouvrement_lease"
        unique_together = ('contrat', 'date_echeance')
        permissions = [
            ("view_all_leases", "Peut voir toutes les échéances de l'entreprise"),
        ]

    def __str__(self):
        return f"Lease {self.contrat.id} - {self.date_echeance} - {self.statut}"



class Paiement(BaseModel):

    @classmethod
    def generer_reference_paiement(cls, methode):
        """
        Génère une référence unique d'audit au format: PREFIX.YYYYMMDD.HHMM.RANDOM
        Exemple Mobile : MOB.20260420.1107.A1B2C3
        Exemple Espèces : ESP.20260420.1107.X9Y8Z7
        """
        # Choix dynamique du préfixe
        prefix = "ESP" if methode == cls.METHODE_ESPECES else "MOB"

        now = timezone.now()
        date_str = now.strftime("%Y%m%d")
        heure_str = now.strftime("%H%M")

        # Génère 6 caractères alphanumériques aléatoires (majuscules + chiffres)
        chars = string.ascii_uppercase + string.digits
        random_suffix = ''.join(random.choice(chars) for _ in range(6))

        return f"{prefix}.{date_str}.{heure_str}.{random_suffix}"


    # --- Constantes de Méthode ---
    METHODE_MOBILE_MONEY = 'MOBILE_MONEY'
    METHODE_ESPECES = 'ESPECES'

    METHODE_CHOICES = [
        (METHODE_MOBILE_MONEY, 'Mobile Money'),
        (METHODE_ESPECES, 'Espèces'),
    ]

    # --- Constantes de Statut ---
    STATUT_EN_ATTENTE = 'EN_ATTENTE'
    STATUT_VALIDE = 'VALIDE'
    STATUT_ECHEC = 'ECHEC'
    STATUT_ANNULE = 'ANNULE'

    STATUT_CHOICES = [
        (STATUT_EN_ATTENTE, 'En attente'),
        (STATUT_VALIDE, 'Validé'),
        (STATUT_ECHEC, 'Échec'),
        (STATUT_ANNULE, 'Annulé'),
    ]

    contrat = models.ForeignKey(
        Contrat,
        on_delete=models.PROTECT,
        related_name="paiements"
    )
    lease = models.ForeignKey(
        Lease,
        on_delete=models.PROTECT,
        related_name="paiements",
        null=True,
        blank=True
    )
    utilisateur = models.ForeignKey(
        CustomUser,
        on_delete=models.PROTECT,
        related_name="paiements_effectues"
    )
    webhook_payload = models.JSONField(
        null=True,
        blank=True,
        help_text="Stocke la copie exacte du dernier webhook reçu du fournisseur."
    )

    montant = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    methode = models.CharField(max_length=20, choices=METHODE_CHOICES)
    date_paiement = models.DateTimeField(null=True, blank=True)

    reference = models.CharField(max_length=255, unique=True)
    transaction_id = models.CharField(max_length=255, null=True, blank=True)

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_EN_ATTENTE)

    class Meta:
        db_table = "recouvrement_paiement"
        permissions = [
            ("can_validate_payment", "Peut valider un paiement manuel (Espèces)"),
            ("can_cancel_payment", "Peut annuler une transaction erronée"),
            ("view_all_paiements", "Peut voir tous les paiements de son entreprise"),
        ]

    def __str__(self):
        return f"Paiement {self.reference} - {self.montant}"