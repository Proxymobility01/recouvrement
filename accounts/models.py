# Create your models here.
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import Permission
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q
from accounts.managers import CustomUserManager


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
    keycloak_id = models.CharField("Keycloak ID", max_length=255, unique=True, db_index=True)

    email = models.EmailField("Email", null=True, blank=True)
    nom_complet = models.CharField("Nom Complet", max_length=255, null=True, blank=True)

    is_active = models.BooleanField("Actif", default=True)
    is_staff = models.BooleanField("Accès Admin", default=False)

    # Remplace PermissionsMixin : Indispensable pour l'Admin Django
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
        ]

    def __str__(self):
        return self.nom_complet or self.keycloak_id

    @property
    def role_principal(self):
        relation = self.assignations_roles.filter(actif=True, principal=True).select_related("role").first()
        return relation.role if relation else None

    def has_role(self, slug: str) -> bool:
        return self.roles.filter(slug=slug, assignations_utilisateurs__actif=True).exists()

    # ==========================================
    # SURCHARGE DES PERMISSIONS DJANGO
    # ==========================================
    def has_perm(self, perm, obj=None):
        """Vérifie si l'utilisateur possède une permission spécifique."""
        if not self.is_active:
            return False
        if self.is_superuser:  # Sécurité : is_staff ne donne plus tous les droits
            return True

        codename = perm.split('.')[-1]
        return self.assignations_roles.filter(
            actif=True,
            role__permissions__codename=codename
        ).exists()

    def has_perms(self, perm_list, obj=None):
        """Vérifie si l'utilisateur possède TOUTES les permissions de la liste (Requis par DRF)."""
        if not self.is_active:
            return False
        if self.is_superuser:
            return True

        return all(self.has_perm(perm, obj) for perm in perm_list)

    def has_module_perms(self, app_label):
        """Vérifie si l'utilisateur a accès à une application entière."""
        if not self.is_active:
            return False
        if self.is_superuser:
            return True

        return self.assignations_roles.filter(
            actif=True,
            role__permissions__content_type__app_label=app_label
        ).exists()


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
            models.Index(fields=["compte_id", "user"]),
            models.Index(fields=["compte_id", "role"]),
            models.Index(fields=["user", "role"]),
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


