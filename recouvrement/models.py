import random
import secrets
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



class SessionPaiement(BaseModel):
    """
    Représente un panier de paiement global regroupant plusieurs échéances.
    C'est cette référence qui est envoyée au fournisseur Mobile Money.
    """
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

    reference = models.CharField(max_length=100, unique=True,)
    transaction_id = models.CharField(max_length=255, null=True, blank=True, help_text="ID transaction fournisseur")
    date_validation = models.DateTimeField(null=True, blank=True)
    montant_total = models.DecimalField(max_digits=12, decimal_places=2)
    telephone = models.CharField(max_length=20, null=True, blank=True)
    webhook_payload = models.JSONField(null=True, blank=True)
    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_EN_ATTENTE)

    utilisateur = models.ForeignKey(
        CustomUser,
        on_delete=models.PROTECT,
        related_name="sessions_initiees"
    )


    class Meta:
        db_table = "recouvrement_session_paiement"



    def __str__(self):
        return f"Session {self.reference} - {self.montant_total} XAF"


class Paiement(BaseModel):
    # --- Constantes de Méthode ---
    METHODE_MOBILE_MONEY = 'MOBILE_MONEY'
    METHODE_ESPECES = 'ESPECES'

    METHODE_CHOICES = [
        (METHODE_MOBILE_MONEY, 'Mobile Money'),
        (METHODE_ESPECES, 'Espèces'),
    ]


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

    contrat = models.ForeignKey(Contrat, on_delete=models.PROTECT, related_name="paiements")
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="paiements", null=True, blank=True)
    utilisateur = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="paiements_effectues")

    # 🚀 CORRECTION 1 : null=True est INDISPENSABLE pour que les espèces fonctionnent
    session = models.ForeignKey(
        SessionPaiement,
        on_delete=models.CASCADE,
        related_name="lignes_paiement",
        null=True,
        blank=True
    )

    montant = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    methode = models.CharField(max_length=20, choices=METHODE_CHOICES)
    reference = models.CharField(max_length=255, unique=True)
    est_annule = models.BooleanField(default=False)
    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_EN_ATTENTE)
    date_paiement = models.DateTimeField(null=True, blank=True)

    @classmethod
    def generer_reference_paiement(cls, methode):
        """Génère une référence unique d'audit cryptographiquement sûre."""
        prefix = "ESP" if methode == cls.METHODE_ESPECES else "MOB"
        now = timezone.now()
        date_str = now.strftime("%Y%m%d")
        heure_str = now.strftime("%H%M%S")  # Ajout des secondes pour plus de précision

        # 🚀 SÉCURITÉ CRYPTOGRAPHIQUE : Remplace random.choice
        random_suffix = secrets.token_hex(3).upper()  # Ex: A1B2C3

        return f"{prefix}.{date_str}.{heure_str}.{random_suffix}"

    class Meta:
        db_table = "recouvrement_paiement"
        permissions = [
            ("can_validate_payment", "Peut valider un paiement manuel (Espèces)"),
            ("can_cancel_payment", "Peut annuler une transaction erronée"),
            ("view_all_paiements", "Peut voir tous les paiements de son entreprise"),
        ]


    @property
    def transaction_id(self):
        """Les espèces n'ont pas d'ID de transaction MTN/Orange."""
        if self.session:
            return self.session.transaction_id
        return None


    def __str__(self):
        return f"Paiement {self.reference} - {self.montant}"