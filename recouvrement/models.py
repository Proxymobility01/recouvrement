import secrets
from django.db.models import Q
from decimal import Decimal

from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from accounts.models import BaseModel, CustomUser
from core.utils import remove_accents


# Create your models here.
# ==========================================
# 4. LOGIQUE MÉTIER : CONTRATS
# ==========================================
class TypeContrat(BaseModel):
    """
    Permet de créer des types de contrats à l'infini (GPS, Parapluie, etc.)
    """
    libelle = models.CharField(max_length=100)

    # 🚀 CORRECTION : On retire unique=True pour laisser la UniqueConstraint faire le job Multi-Tenant
    code = models.SlugField(max_length=100)

    est_principal = models.BooleanField(
        default=False,
        help_text="Cochez si c'est un contrat principal (ex: Véhicule). Laissez décoché pour des accessoires."
    )

    class Meta:
        db_table = "recouvrement_type_contrat"
        constraints = [
            # 🚀 C'est elle qui garantit l'unicité par entreprise !
            models.UniqueConstraint(fields=['code', 'compte_id'], name='unique_type_contrat_code_par_compte')
        ]

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.strip().upper()
        if self.libelle:
            self.libelle = self.libelle.strip()

        super().save(*args, **kwargs)

    def __str__(self):
        return self.libelle


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
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contrats_enregistres",
        help_text="L'agent (PARTNER_ADMIN) qui a saisi ce contrat dans le système."
    )

    type_contrat = models.ForeignKey(
        TypeContrat,
        on_delete=models.PROTECT,
        related_name="contrats",
        null=True,
        help_text="La catégorie de ce contrat (Véhicule, Téléphone, etc.)"
    )


    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sous_contrats",
        help_text="Si ce contrat est un accessoire, sélectionnez le contrat principal ici."
    )

    nom_complet = models.CharField(max_length=255)
    nom_complet_search = models.CharField(max_length=255, null=True, blank=True)
    reference = models.CharField(
        "Référence du contrat",
        max_length=50,
        unique=True,
        blank=True,
        help_text="Généré automatiquement (ex: RYLVEH202600012)"
    )
    immatriculation = models.CharField(max_length=20,null=True, blank=True)
    vin = models.CharField("Numéro de châssis (VIN)", max_length=17, null=True, blank=True)

    specificites = models.JSONField(
        "Spécificités Techniques (JSON)",
        default=None,
        blank=True,
        null=True,
        help_text="Attributs variables (ex: {'marque': 'Samsung', 'imei': '123...'})"
    )

    montant_total = models.DecimalField(max_digits=12, decimal_places=2)
    montant_restant = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    montant_par_paiement = models.DecimalField(max_digits=12, decimal_places=2)

    frequence = models.CharField(max_length=50,choices=FREQUENCE_CHOICES, default=JOURNALIER)
    date_debut = models.DateField()
    date_fin = models.DateField()
    prochaine_echeance = models.DateField()

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_ACTIF)

    class Meta:
        db_table = "recouvrement_contrat"
        constraints = [
            models.UniqueConstraint(
                fields=['chauffeur'],
                condition=Q(parent__isnull=True) & ~Q(statut__in=['SOLDE', 'ANNULE']),
                name='uniq_contrat_parent_actif_par_chauffeur'
            )
        ]
        permissions = [
            ("view_all_contrats", "Peut voir tous les contrats de son entreprise"),
        ]
        indexes = [
            models.Index(fields=['-created_at'], name='idx_contrat_created_at'),
            GinIndex(fields=['nom_complet_search'], name='idx_cntr_search_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['reference'], name='idx_cntr_ref_trgm', opclasses=['gin_trgm_ops']),
            models.Index(fields=['immatriculation'], name='idx_contrat_immat'),
            models.Index(fields=['vin'], name='idx_contrat_vin'),
            models.Index(fields=['compte_id', '-created_at'], name='idx_contrat_tenant_date'),
        ]


    def __str__(self):
        return f"Contrat {self.id} - {self.nom_complet}"


    def generer_reference(self):
        """
        Génère une référence unique au format SANS TIRET : RYL[CODE_TYPE][ANNÉE][SÉQUENCE]
        Exemple : RYLVEH202600001
        """
        code_client = "RYL"

        # On utilise le code du type de contrat (limité à 3 lettres max), ou "DIV" par défaut
        type_code = self.type_contrat.code.upper()[:3] if self.type_contrat and self.type_contrat.code else "DIV"

        # On utilise l'année de la date de début
        annee = self.date_debut.year if self.date_debut else timezone.now().year

        # 🚀 Le nouveau préfixe SANS tiret (ex: RYLVEH2026)
        prefix = f"{code_client}{type_code}{annee}"

        with transaction.atomic():
            dernier_contrat = Contrat.objects.select_for_update().filter(
                reference__startswith=prefix
            ).order_by('-reference').first()

            if dernier_contrat and dernier_contrat.reference:
                # 🚀 ASTUCE PYTHON : On coupe le texte pour ne garder que ce qui vient APRÈS le préfixe
                # Si prefix est long de 10 caractères, on prend du 10ème caractère jusqu'à la fin
                derniere_sequence_str = dernier_contrat.reference[len(prefix):]

                try:
                    nouvelle_sequence = int(derniere_sequence_str) + 1
                except ValueError:
                    nouvelle_sequence = 1
            else:
                nouvelle_sequence = 1

            # On formate sur 5 chiffres (ex: 1 devient 00001)
            return f"{prefix}{nouvelle_sequence:05d}"
    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.generer_reference()
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        # S'assure que si on met à jour uniquement 'nom_complet', on met aussi à jour la recherche
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'nom_complet' in update_fields:
            update_fields = set(update_fields)
            update_fields.add('nom_complet_search')
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    @property
    def has_sous_contrat(self):
        """
        Remplace avantageusement une colonne booléenne.
        Sera calculé à la volée.
        """
        return self.sous_contrats.exists()

    @property
    def montant_verse(self):
        """
        Champ virtuel : Calcule dynamiquement l'argent déjà encaissé.
        """
        if self.montant_total is not None and self.montant_restant is not None:
            return self.montant_total - self.montant_restant
        return 0

    @property
    def montant_paye(self):
        """
        Calcule dynamiquement le montant total déjà payé.
        Évite de faire une requête SUM() sur les paiements ou les leases,
        ce qui optimise grandement les performances (O(1)).
        """
        if self.montant_total is not None and self.montant_restant is not None:
            return self.montant_total - self.montant_restant
        return 0

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
        indexes = [
            models.Index(fields=['compte_id', '-created_at'], name='idx_spaie_tenant_date'),
        ]



    def __str__(self):
        return f"Session {self.reference} - {self.montant_total} XAF"

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

    nom_complet = models.CharField(max_length=255, null=True, blank=True)
    nom_complet_search = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    class Meta:
        db_table = "recouvrement_lease"
        permissions = [
            ("view_all_leases", "Peut voir toutes les échéances de l'entreprise"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['contrat', 'date_echeance'],
                name='unique_lease_par_contrat_et_date'
            )
        ]
        indexes = [
            models.Index(fields=['-created_at'], name='idx_lease_created_at'),
            GinIndex(fields=['nom_complet_search'], name='idx_lease_search_trgm', opclasses=['gin_trgm_ops']),
            models.Index(fields=['compte_id', '-created_at'], name='idx_lease_tenant_date'),
        ]

    def save(self, *args, **kwargs):
        # 1. On aspire le nom du contrat si on ne l'a pas encore
        if not self.nom_complet and self.contrat_id:
            self.nom_complet = self.contrat.nom_complet

        # 2. On génère la version de recherche
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        # 3. Gestion des update_fields pour la performance
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            if 'nom_complet' in update_fields or 'contrat' in update_fields:
                update_fields.add('nom_complet')
                update_fields.add('nom_complet_search')
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Lease {self.contrat.id} - {self.date_echeance} - {self.statut}"


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

    nom_complet = models.CharField(max_length=255, null=True, blank=True)
    nom_complet_search = models.CharField(max_length=255, null=True, blank=True)

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

    def save(self, *args, **kwargs):
        # 1. On aspire le nom depuis le contrat lié au paiement
        if not self.nom_complet and self.contrat_id:
            self.nom_complet = self.contrat.nom_complet

        # 2. On génère la version de recherche
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        # 3. Gestion des update_fields
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            if 'nom_complet' in update_fields or 'contrat' in update_fields:
                update_fields.add('nom_complet')
                update_fields.add('nom_complet_search')
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    class Meta:
        db_table = "recouvrement_paiement"
        permissions = [
            ("can_validate_payment", "Peut valider un paiement manuel (Espèces)"),
            ("can_cancel_payment", "Peut annuler une transaction erronée"),
            ("view_all_paiements", "Peut voir tous les paiements de son entreprise"),
        ]
        indexes = [
            models.Index(fields=['-created_at'], name='idx_paie_created_at'),
            models.Index(fields=['compte_id', '-created_at'], name='idx_paie_tenant_date'),
            GinIndex(fields=['nom_complet_search'], name='idx_paie_search_trgm', opclasses=['gin_trgm_ops']),
        ]


    @property
    def transaction_id(self):
        """Les espèces n'ont pas d'ID de transaction MTN/Orange."""
        if self.session:
            return self.session.transaction_id
        return None


    def __str__(self):
        return f"Paiement {self.reference} - {self.montant}"







