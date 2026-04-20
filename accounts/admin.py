# Register your models here.
from django.contrib import admin
from .models import Role, CustomUser, CustomUserRole


# ==========================================
# 1. ADMINISTRATION DES ROLES
# ==========================================
@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('libelle', 'slug', 'niveau')
    prepopulated_fields = {'slug': ('libelle',)}  # Remplit automatiquement le slug
    search_fields = ('libelle', 'slug')

    # INDISPENSABLE pour les ManyToMany (permissions)
    # Cela crée une interface avec deux colonnes (disponibles / choisies) au lieu d'une simple liste déroulante
    filter_horizontal = ('permissions',)


# ==========================================
# 2. INLINE : AFFECTATION DES ROLES
# ==========================================
class CustomUserRoleInline(admin.TabularInline):
    """
    Permet d'ajouter, modifier ou supprimer les rôles directement
    depuis la page de profil d'un CustomUser.
    """
    model = CustomUserRole
    extra = 1  # Nombre de lignes vides affichées par défaut
    fk_name = 'user'

    # Utilise une barre de recherche au lieu d'un select si tu as beaucoup d'utilisateurs/rôles
    autocomplete_fields = ['role', 'assigne_par']

    # On affiche les champs pertinents dans le tableau
    fields = ('role', 'principal', 'actif', 'compte_id', 'assigne_par')


# ==========================================
# 3. ADMINISTRATION DES UTILISATEURS
# ==========================================
@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    # Ce qui s'affiche dans le tableau principal
    list_display = ('keycloak_id', 'nom_complet', 'email', 'compte_id', 'is_active', 'is_staff', 'is_superuser')

    # Filtres latéraux
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'compte_id')

    # Barre de recherche (utilisée aussi pour l'autocomplete_fields de l'inline)
    search_fields = ('keycloak_id', 'email', 'nom_complet')

    # On intègre le tableau des rôles défini plus haut
    inlines = [CustomUserRoleInline]

    # Organisation de la page de détail en sections claires
    fieldsets = (
        ('Identité (Géré par Keycloak)', {
            'fields': ('keycloak_id',)
        }),
        ('Informations Personnelles', {
            'fields': ('nom_complet', 'email', 'compte_id')
        }),
        ('Accès & Sécurité Django', {
            'fields': ('is_active', 'is_staff', 'is_superuser'),
            'description': "Attention : 'Super Admin' donne tous les droits sans vérifier les rôles ci-dessous."
        }),
    )

    # Si ton BaseModel possède created_at / updated_at, tu peux les afficher en lecture seule
    # readonly_fields = ('created_at', 'updated_at')