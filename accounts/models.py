# Create your models here.
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import Permission
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q
from accounts.managers import CustomUserManager
from core.utils import remove_accents


class BaseModel(models.Model):
    compte_id = models.IntegerField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ==========================================
# 2. IDENTITÉ ET RÔLES (Miroir local pour Recouvrement)
# ==========================================
class Role(models.Model):
    libelle = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    niveau = models.PositiveSmallIntegerField(default=0)

    # Lien direct avec les permissions natives de Django
    permissions = models.ManyToManyField(Permission, related_name='roles_recouvrement', blank=True)

    class Meta:
        db_table = "account_role"
        indexes = [
            models.Index(fields=["slug"], name="idx_rec_role_slug"),
        ]

    def __str__(self):
        return self.libelle


class CustomUser(AbstractBaseUser, BaseModel):
    keycloak_id = models.CharField("Keycloak ID", max_length=255, unique=True)

    email = models.EmailField("Email", null=True, blank=True)
    nom_complet = models.CharField("Nom Complet", max_length=255)
    nom_complet_search = models.CharField( max_length=255, null=True, blank=True)
    is_active = models.BooleanField("Actif", default=True)
    is_staff = models.BooleanField("Accès Admin", default=False)
    is_superuser = models.BooleanField("Super Admin", default=False)

    roles = models.ManyToManyField(
        "Role",
        through="CustomUserRole",
        through_fields=("user", "role"),
        related_name="users",
        blank=True
    )

    objects = CustomUserManager()

    USERNAME_FIELD = 'keycloak_id'
    REQUIRED_FIELDS = ['compte_id', 'email']

    class Meta:
        db_table = "account_customuser"
        indexes = [
            models.Index(fields=["compte_id", "is_active"]),
            models.Index(fields=["keycloak_id"], name="idx_rec_user_kc_id"),
            GinIndex(fields=['nom_complet_search'], name='idx_user_search_trgm', opclasses=['gin_trgm_ops']),
        ]
        permissions = [
            ("view_all_users", "Peut voir tous les utilisateurs de son entreprise (Tenant)"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['email'],
                condition=~Q(email__isnull=True) & ~Q(email__exact=''),  # S'applique si non-null ET non-vide
                name='unique_email_if_not_null'
            )
        ]

    def __str__(self):
        return self.nom_complet or self.keycloak_id

    @property
    def role_principal(self):
        relation = self.assignations_roles.filter(actif=True, principal=True).select_related("role").first()
        return relation.role if relation else None

    def has_role(self, slug: str) -> bool:
        return self.roles.filter(slug=slug, assignations_utilisateurs__actif=True).exists()

    def save(self, *args, **kwargs):
        # 🚀 CORRECTION 2 : Éviter le bug "None" et passer en minuscules
        if self.nom_complet:
            self.nom_complet_search = remove_accents(self.nom_complet).lower()
        else:
            self.nom_complet_search = ""
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'nom_complet' in update_fields:
            update_fields = set(update_fields)
            update_fields.add('nom_complet_search')
            kwargs['update_fields'] = list(update_fields)

        super().save(*args, **kwargs)

    # ==========================================
    # SURCHARGE DES PERMISSIONS DJANGO
    # ==========================================
    def has_perm(self, perm, obj=None):
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        if not hasattr(self, '_perm_cache'):
            self._perm_cache = set(
                self.assignations_roles.filter(actif=True)
                .values_list('role__permissions__codename', flat=True)
            )
        return perm.split('.')[-1] in self._perm_cache

    def has_perms(self, perm_list, obj=None):
        """Vérifie si l'utilisateur possède TOUTES les permissions de la liste (Requis par DRF)."""
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        return all(self.has_perm(perm, obj) for perm in perm_list)

    def has_module_perms(self, app_label):
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        if not hasattr(self, '_module_perm_cache'):
            self._module_perm_cache = set(
                self.assignations_roles.filter(actif=True)
                .values_list('role__permissions__content_type__app_label', flat=True)
            )
        return app_label in self._module_perm_cache


# ==========================================
# 3. TABLE D'AFFECTATION UTILISATEUR <-> ROLE
# ==========================================
class CustomUserRole(BaseModel):
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="assignations_roles"
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="assignations_utilisateurs"
    )

    principal = models.BooleanField(default=False)
    actif = models.BooleanField(default=True)

    assigne_par = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="roles_assignes"
    )

    class Meta:
        db_table = "account_customuser_role"
        constraints = [
            models.UniqueConstraint(
                fields=["compte_id", "user", "role"],
                name="uniq_role_par_user_par_compte_rec"
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(principal=True),
                name="uniq_role_principal_par_user_rec"
            )
        ]
        indexes = [
            models.Index(fields=["compte_id", "actif"]),
        ]

    def clean(self):
        super().clean()

        if self.user_id and self.compte_id and self.user.compte_id != self.compte_id:
            raise ValidationError({
                "user": "L'utilisateur doit appartenir au même compte que l'affectation."
            })

        if self.assigne_par_id and self.compte_id and self.assigne_par.compte_id != self.compte_id:
            raise ValidationError({
                "assigne_par": "L'assignateur doit appartenir au même compte."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} -> {self.role}"


class ConfigPaiement(BaseModel):
    """
    Stocke les identifiants, URLs et clés de sécurité de la passerelle
    Mobile Money spécifiques à chaque entreprise (Tenant).
    """
    api_key = models.CharField(
        "Clé d'API Passerelle",
        max_length=255,
        help_text="Clé API fournie par PayGate (ex: Api-Key...)"
    )
    base_url = models.URLField(
        "URL de base de l'API",
        help_text="Ex de format: https://api.paygate.cm"
    )
    success_url = models.URLField(
        "URL de redirection en cas de succès",
        help_text="URL vers laquelle le chauffeur est redirigé après son paiement réussi."
    )

    webhook_secret = models.CharField(
        "Secret de validation du Webhook",
        max_length=255,
        help_text="Clé secrète partagée pour vérifier la signature HMAC (X-Signature) des notifications reçues."
    )

    class Meta:
        db_table = "account_config_paiement"
        constraints = [
            # Sécurité majeure : Une seule configuration de paiement active par entreprise
            models.UniqueConstraint(
                fields=['compte_id'],
                name='unique_config_paiement_par_compte'
            )
        ]

    def __str__(self):
        return f"Configuration Paiement - Compte {self.compte_id}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Vider le cache à chaque mise à jour de la configuration
        from django.core.cache import cache
        cache.delete(f"credentials_{self.compte_id}")

    def delete(self, *args, **kwargs):
        from django.core.cache import cache
        cache.delete(f"credentials_{self.compte_id}")
        super().delete(*args, **kwargs)