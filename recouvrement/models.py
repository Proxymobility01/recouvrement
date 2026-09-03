import secrets
from django.db.models import Q
from decimal import Decimal
from django.contrib.postgres.indexes import BTreeIndex, GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone
from django_q.models import Schedule
from accounts.models import BaseModel, ConfigPaiement, CustomUser
from core.utils import remove_accents


# Create your models here.
# ==========================================
# 4. LOGIQUE MÉTIER : CONTRATS
# ==========================================
class Agence(BaseModel):
    """Agence opérationnelle appartenant à un compte partenaire."""

    nom = models.CharField(max_length=150)
    nom_search = models.CharField(max_length=255, null=True, blank=True)
    code = models.SlugField(max_length=50)
    adresse = models.TextField(blank=True)
    telephone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    actif = models.BooleanField(default=True)

    class Meta:
        db_table = "rc_agence"
        verbose_name = "Agence"
        verbose_name_plural = "Agences"
        constraints = [
            models.UniqueConstraint(
                fields=['compte_id', 'code'],
                name='unique_agence_code_par_compte',
            ),
        ]
        indexes = [
            GinIndex(fields=['nom_search'], name='idx_agence_nom_search_trgm', opclasses=['gin_trgm_ops']),
            models.Index(
                fields=['compte_id', 'actif'],
                name='idx_agence_compte_actif',
            ),
        ]

    def save(self, *args, **kwargs):
        # 1. Nettoyage des champs de base
        if self.nom:
            self.nom = self.nom.strip()
            # Génération du champ de recherche sans accents et en minuscules
            self.nom_search = remove_accents(self.nom).lower()
        else:
            self.nom_search = ""

        if self.code:
            self.code = self.code.strip().upper()

        # 2. Gestion intelligente des update_fields pour les performances
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

            # Si on modifie le nom, on force la sauvegarde du nom_search
            if 'nom' in update_fields:
                update_fields.add('nom_search')

            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} - {self.nom}"


class AgenceScopedModel(BaseModel):
    """Base abstraite des données opérationnelles rattachées à une agence."""

    agence = models.ForeignKey(
        Agence,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)ss",
        null=True,
        blank=True,
        help_text=(
            "Agence propriétaire de cette donnée. Le champ reste "
            "temporairement facultatif pendant la reprise de l'historique."
        ),
    )

    class Meta:
        abstract = True


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
        db_table = "rc_type_contrat"
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


class Contrat(AgenceScopedModel):
    # --- Constantes de Statut ---
    STATUT_ACTIF = 'ACTIF'
    STATUT_SUSPENDU = 'SUSPENDU'
    STATUT_SOLDE = 'SOLDE'
    STATUT_CONTENTIEUX = 'CONTENTIEUX'

    # Une règle de génération ne peut être attribuée en masse qu'aux
    # contrats susceptibles de générer des leases.
    STATUTS_ASSIGNABLES_REGLE_GENERATION = (STATUT_ACTIF,)


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
    montant_paye = models.DecimalField(max_digits=12,decimal_places=2,default=0)

    frequence = models.CharField(max_length=50,choices=FREQUENCE_CHOICES, default=JOURNALIER)
    date_debut = models.DateField()
    date_fin = models.DateField()
    prochaine_echeance = models.DateTimeField(null=True, blank=True)

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_ACTIF)

    regle_penalite = models.ForeignKey(
        'ReglePenalite',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contrats",
        verbose_name="Règle de pénalité appliquée",
        help_text="Associer une règle pour activer la planification automatique des pénalités de retard."
    )

    regle_generation = models.ForeignKey(
        'RegleGenerationLease',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="contrats",
        verbose_name="Règle de génération"
    )

    config_paiement = models.ForeignKey(
        'accounts.ConfigPaiement',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='contrats',
        verbose_name="Configuration de paiement",
        help_text=(
            "Configuration utilisée pour les paiements Mobile Money. "
            "Elle peut rester vide pour les autres moyens de paiement."
        ),
    )

    class Meta:
        db_table = "rc_contrat"
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
        est_creation = self._state.adding
        ancienne_config_paiement_id = None
        if not est_creation and self.parent_id is None:
            ancienne_config_paiement_id = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list('config_paiement_id', flat=True)
                .first()
            )

        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

        # Chaque contrat (parent ou sous-contrat) choisit sa règle de manière
        # indépendante. Une règle explicite est conservée ; sinon, uniquement
        # lors de la création, on utilise la règle par défaut de son compte.
        if (
            self._state.adding
            and self.regle_generation_id is None
            and self.compte_id is not None
        ):
            self.regle_generation = (
                RegleGenerationLease.objects
                .filter(compte_id=self.compte_id, defaut=True)
                .first()
            )
            if update_fields is not None:
                update_fields.add('regle_generation')

        # Un sous-contrat hérite toujours de l'agence et de la configuration
        # de paiement de son parent. Ces deux attributs ne sont jamais choisis
        # indépendamment au niveau du sous-contrat.
        if self.parent_id:
            agence_parent_id = self.parent.agence_id
            if self.agence_id != agence_parent_id:
                self.agence_id = agence_parent_id
                if update_fields is not None:
                    update_fields.add('agence')

            config_parent_id = self.parent.config_paiement_id
            if self.config_paiement_id != config_parent_id:
                self.config_paiement_id = config_parent_id
                if update_fields is not None:
                    update_fields.add('config_paiement')
        elif (
            est_creation
            and self.config_paiement_id is None
            and self.compte_id is not None
        ):
            # Une configuration explicitement choisie sur le parent reste
            # prioritaire. Sinon, le défaut du compte est appliqué.
            self.config_paiement = (
                ConfigPaiement.objects
                .filter(compte_id=self.compte_id, defaut=True)
                .first()
            )
            if update_fields is not None:
                update_fields.add('config_paiement')

        if (
            self.config_paiement_id is not None
            and self.config_paiement.compte_id != self.compte_id
        ):
            raise ValidationError({
                'config_paiement': (
                    "La configuration de paiement doit appartenir au même "
                    "compte que le contrat."
                ),
            })

        if not self.reference:
            self.reference = self.generer_reference()
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        # S'assure que si on met à jour uniquement 'nom_complet', on met aussi à jour la recherche
        if update_fields is not None:
            if 'nom_complet' in update_fields:
                update_fields.add('nom_complet_search')

            # Si on met à jour les finances, on force la sauvegarde du montant payé
            if 'montant_total' in update_fields or 'montant_restant' in update_fields:
                update_fields.add('montant_paye')

            kwargs['update_fields'] = list(update_fields)

        config_paiement_sauvegardee = (
            update_fields is None or 'config_paiement' in update_fields
        )

        with transaction.atomic():
            super().save(*args, **kwargs)

            if (
                not est_creation
                and self.parent_id is None
                and config_paiement_sauvegardee
                and ancienne_config_paiement_id != self.config_paiement_id
            ):
                self.sous_contrats.update(
                    config_paiement_id=self.config_paiement_id,
                    updated_at=timezone.now(),
                )

    @property
    def has_sous_contrat(self):
        """
        Remplace avantageusement une colonne booléenne.
        Sera calculé à la volée.
        """
        return self.sous_contrats.exists()


class SessionPaiement(AgenceScopedModel):
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
    gateway_reference = models.CharField(max_length=255, null=True, blank=True)
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

    config_paiement = models.ForeignKey(
        'accounts.ConfigPaiement',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='sessions_paiement',
        verbose_name="Configuration de paiement utilisée",
        help_text=(
            "Configuration ayant servi à créer une transaction Mobile "
            "Money. Elle peut rester vide pour les autres paiements."
        ),
    )

    @classmethod
    def generer_reference_session(cls) -> str:
        """Génère une référence de session unique, cryptographiquement sûre."""
        now = timezone.now()
        date_str = now.strftime("%Y%m%d")
        heure_str = now.strftime("%H%M%S")
        random_suffix = secrets.token_hex(3).upper()
        return f"MOB.{date_str}.{heure_str}.{random_suffix}"

    class Meta:
        db_table = "rc_session_paiement"
        permissions = [
            ("view_all_sessionpaiements", "Peut voir toutes les sessions de paiement du compte"),
        ]
        indexes = [
            models.Index(fields=['compte_id', '-created_at'], name='idx_spaie_tenant_date'),
            GinIndex(
                fields=['reference'],
                name='idx_spaie_ref_trgm',
                opclasses=['gin_trgm_ops']
            ),
            GinIndex(
                fields=['telephone'],
                name='idx_spaie_tel_trgm',
                opclasses=['gin_trgm_ops']
            ),

        ]



    def __str__(self):
        return f"Session {self.reference} - {self.montant_total} XAF"


class Lease(AgenceScopedModel):
    # --- Constantes de Statut ---
    STATUT_NON_PAYE = 'NON_PAYE'
    STATUT_PARTIEL = 'PARTIEL'
    STATUT_PAYE = 'PAYE'
    STATUT_ANNULE = 'ANNULE'

    STATUT_CHOICES = [
        (STATUT_NON_PAYE, 'Non payé'),
        (STATUT_PARTIEL, 'Partiellement payé'),
        (STATUT_PAYE, 'Payé'),
        (STATUT_ANNULE, 'Annulé (Absence/Panne)'),
    ]

    contrat = models.ForeignKey(
        Contrat,
        on_delete=models.PROTECT,
        related_name="leases",
        help_text="Le contrat auquel cette échéance journalière est rattachée."
    )

    date_echeance = models.DateTimeField(db_index=True)

    montant_attendu = models.DecimalField(max_digits=12, decimal_places=2)

    montant_paye = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_NON_PAYE)

    nom_complet = models.CharField(max_length=255, null=True, blank=True)
    nom_complet_search = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    class Meta:
        db_table = "rc_lease"
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
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

        # 🏢 1. Propagation automatique de l'agence
        if not self.agence_id and self.contrat_id:
            self.agence_id = self.contrat.agence_id
            if update_fields is not None:
                update_fields.add('agence')

        # 2. On aspire le nom du contrat si on ne l'a pas encore
        if not self.nom_complet and self.contrat_id:
            self.nom_complet = self.contrat.nom_complet
            if update_fields is not None:
                update_fields.add('nom_complet')

        # 3. On génère la version de recherche
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        if update_fields is not None and 'nom_complet' in update_fields:
            update_fields.add('nom_complet_search')

        if update_fields is not None:
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Lease {self.contrat.id} - {self.date_echeance} - {self.statut}"


class Paiement(AgenceScopedModel):
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
    enregistre_par = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name="paiements_effectues")

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
    est_annule = models.BooleanField(default=False)
    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_EN_ATTENTE)
    date_paiement = models.DateTimeField(null=True, blank=True)

    nom_complet = models.CharField(max_length=255, null=True, blank=True)
    nom_complet_search = models.CharField(max_length=255, null=True, blank=True)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

        # 🏢 1. Propagation automatique de l'agence depuis le contrat
        if not self.agence_id and self.contrat_id:
            self.agence_id = self.contrat.agence_id
            if update_fields is not None:
                update_fields.add('agence')

        # 2. On aspire le nom depuis le contrat
        if not self.nom_complet and self.contrat_id:
            self.nom_complet = self.contrat.nom_complet
            if update_fields is not None:
                update_fields.add('nom_complet')

        # 3. On génère la version de recherche
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        if update_fields is not None and 'nom_complet' in update_fields:
            update_fields.add('nom_complet_search')

        if update_fields is not None:
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    class Meta:
        db_table = "rc_paiement"
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
            return self.session.reference
        return None


    def __str__(self):
        session_ref = self.session.reference if self.session else "sans session"
        return f"Paiement #{self.pk} ({session_ref}) - {self.montant}"


class Parametre(BaseModel):
    """
    Configuration globale des règles métier pour une entreprise (tenant).
    """
    # Stocke une liste d'entiers. Ex: [6] pour Dimanche, [5, 6] pour le Week-end
    jours_repos = models.JSONField(
        default=list,
        help_text="Liste des jours ignorés pour les échéances (0=Lundi, ..., 6=Dimanche). Ex: [6] pour sauter le dimanche."
    )

    class Meta:
        db_table = "rc_parametre"
        constraints = [
            # Une seule ligne de paramètres par entreprise !
            models.UniqueConstraint(fields=['compte_id'], name='unique_param_par_compte')
        ]

    def __str__(self):
        return f"Paramètres du compte {self.compte_id}"


class ReglePenalite(BaseModel):
    nom = models.CharField(
        max_length=100, unique=True,
        help_text="Ex: Tous les jours à 15h, Hebdomadaire le lundi..."
    )

    nom_search = models.CharField(max_length=255, null=True, blank=True)

    # --- Paramètres Financiers ---
    montant = models.DecimalField(
        max_digits=10, decimal_places=2,
        verbose_name="Montant de la pénalité (FCFA)"
    )
    # -1 pour l'infini, 0 pour ne pas s'exécuter, ou un nombre précis (2, 3...)
    occurrences = models.IntegerField(
        default=-1,
        help_text="Nombre maximum de pénalités. Mettre -1 pour infini, 0 pour désactiver."
    )

    # --- Paramètres de Planification (Django-Q) ---
    TYPE_CHOICES = [
        (Schedule.ONCE, 'Une seule fois'),
        (Schedule.HOURLY, 'Toutes les heures'),
        (Schedule.DAILY, 'Tous les jours'),
        (Schedule.WEEKLY, 'Toutes les semaines'),
        (Schedule.MONTHLY, 'Tous les mois'),
        (Schedule.CRON, 'Expression Cron (Avancé)'),
    ]
    frequence = models.CharField(
        max_length=1,
        choices=TYPE_CHOICES,
        default=Schedule.DAILY,
        verbose_name="Fréquence d'exécution"
    )
    cron_expression = models.CharField(
        max_length=100,
        blank=True, null=True,
        help_text="Ex: '0 10,14,18 * * *' (Requis si Fréquence = Cron)."
    )

    # 🕒 Ton nouveau champ pour forcer le départ exact
    debut = models.DateTimeField(
        verbose_name="Date et heure de première exécution",
        help_text="Détermine le moment exact (Date + Heure) où la tâche commencera son cycle."
    )

    class Meta:
        db_table = "rc_regle_penalite"
        verbose_name = "Règle de Pénalité"
        verbose_name_plural = "Règles de Pénalité"
        indexes = [
            models.Index(fields=['frequence'], name='idx_regle_frequence'),
            GinIndex(fields=['nom_search'], name='idx_nom_search_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f"{self.nom} ({self.montant} FCFA)"

    def save(self, *args, **kwargs):
        if self.nom:
            self.nom_search = remove_accents(self.nom).lower()
        else:
            self.nom_search = ""
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            if 'nom' in update_fields:
                update_fields.add('nom_search')

            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)


class Penalite(AgenceScopedModel):
    STATUT_NON_PAYE = 'NON_PAYE'
    STATUT_PARTIEL = 'PARTIEL'
    STATUT_PAYE = 'PAYE'

    STATUT_CHOICES = [
        (STATUT_NON_PAYE, 'Non payé'),
        (STATUT_PARTIEL, 'Partiellement payé'),
        (STATUT_PAYE, 'Payé'),
    ]



    lease = models.ForeignKey(
        'Lease',
        on_delete=models.PROTECT,
        related_name="penalites",
        verbose_name="Échéance associée"
    )

    nom_complet = models.CharField(
        max_length=255,
        verbose_name="Nom complet du chauffeur",
        help_text="Nom figé au moment de l'application de la pénalité pour l'audit."
    )

    # 🔍 NOUVEAU : Champ technique invisible pour la recherche rapide
    nom_complet_search = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Nom complet (Recherche optimisée)"
    )

    montant = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Montant de la pénalité (FCFA)"
    )

    date_application = models.DateTimeField(
        default=timezone.now,
        verbose_name="Date et heure de sanction"
    )

    statut = models.CharField(max_length=20, choices=STATUT_CHOICES, default=STATUT_NON_PAYE)

    motif = models.CharField(
        max_length=255,
        verbose_name="Motif / Justification",
        help_text="Ex: Première pénalité de retard, 2ème occurrence..."
    )

    class Meta:
        db_table = "rc_penalite"
        ordering = ['-date_application']
        verbose_name = "Pénalité de retard"
        verbose_name_plural = "Pénalités de retard"
        permissions = [
            ("view_all_penalites", "Peut voir toutes les pénalités de l'entreprise"),
        ]
        indexes = [
            models.Index(fields=['-date_application'], name='idx_penalite_date_desc'),
            models.Index(fields=['lease', '-date_application'], name='idx_penalite_lease_date'),
            GinIndex(
                fields=['nom_complet_search'],
                name='idx_penal_search_trgm',
                opclasses=['gin_trgm_ops']
            ),
        ]

    def __str__(self):
        date_str = self.date_application.strftime('%d/%m/%Y %H:%M') if self.date_application else "—"
        return f"Pénalité de {self.montant} FCFA - {self.nom_complet} ({date_str})"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)

        # 🏢 1. Propagation automatique de l'agence depuis le Lease
        if not self.agence_id and self.lease_id:
            # On utilise select_related si possible en amont, sinon ça fait une petite requête
            self.agence_id = self.lease.agence_id
            if update_fields is not None:
                update_fields.add('agence')

        # 2. Génération automatique du champ de recherche nettoyé
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""

        if update_fields is not None and 'nom_complet' in update_fields:
            update_fields.add('nom_complet_search')

        if update_fields is not None:
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)


class RegleGenerationLease(BaseModel):
    """
    Catalogue des règles de génération de facturation.
    Une règle peut être appliquée à une infinité de contrats.
    """
    nom = models.CharField(
        max_length=100,
        help_text="Ex: 'Classique 24h', 'Demi-journée 12h/22h', 'Hebdomadaire'..."
    )

    nom_search = models.CharField(max_length=255, null=True, blank=True)

    # --- Paramètres de Planification (Django-Q) ---
    TYPE_CHOICES = [
        (Schedule.ONCE, 'Une seule fois'),
        (Schedule.HOURLY, 'Toutes les heures'),
        (Schedule.DAILY, 'Tous les jours'),
        (Schedule.WEEKLY, 'Toutes les semaines'),
        (Schedule.MONTHLY, 'Tous les mois'),
        (Schedule.CRON, 'Expression Cron (Avancé)'),
    ]
    frequence = models.CharField(
        max_length=1,
        choices=TYPE_CHOICES,
        default=Schedule.DAILY,
        verbose_name="Fréquence d'exécution"
    )

    cron_expression = models.CharField(
        max_length=100,
        blank=True, null=True,
        help_text="Ex: '0 12,22 * * *' (Requis si Fréquence = Cron)."
    )

    # 🕒 Déclencheur initial (Crucial pour ONCE, HOURLY et DAILY)
    debut = models.DateTimeField(
        verbose_name="Date et heure de première exécution",
        help_text="Détermine le moment exact (Date + Heure) où le cycle commencera."
    )

    actif = models.BooleanField(
        default=True,
        verbose_name="Règle active",
        help_text="Si désactivé, AUCUN contrat lié à cette règle ne sera facturé."
    )

    defaut = models.BooleanField(
        default=False,
        verbose_name="Règle par défaut",
        help_text="Si coché, cette règle sera automatiquement appliquée aux nouveaux contrats."
    )

    class Meta:
        db_table = "rc_regle_generation_lease"
        verbose_name = "Règle de génération de leases"
        verbose_name_plural = "Règles de génération de leases"
        constraints = [
            models.UniqueConstraint(
                fields=['compte_id', 'nom'],
                name='unique_regle_generation_nom_par_compte',
            ),
            models.UniqueConstraint(
                fields=['compte_id'],
                condition=Q(defaut=True),
                name='unique_regle_generation_defaut_par_compte',
            ),
        ]
        indexes = [
            models.Index(fields=['frequence'], name='idx_regle_lease_freq'),
            GinIndex(fields=['nom_search'], name='idx_lease_nom_search_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f"{self.nom}"


    def save(self, *args, **kwargs):
        if self.nom:
            self.nom_search = remove_accents(self.nom).lower()
        else:
            self.nom_search = ""
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            if 'nom' in update_fields:
                update_fields.add('nom_search')

            kwargs['update_fields'] = list(update_fields)

        with transaction.atomic():
            if self.defaut:
                RegleGenerationLease.objects.filter(
                    compte_id=self.compte_id
                ).exclude(pk=self.pk).update(defaut=False)

            super().save(*args, **kwargs)


