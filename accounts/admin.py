from django.contrib import admin
from django.contrib.auth.models import Permission
from .models import Role, CustomUser, CustomUserRole


# --- INLINES ---

class CustomUserRoleInline(admin.TabularInline):
    """
    Permet d'ajouter/modifier les rôles directement
    depuis la fiche de l'utilisateur.
    """
    model = CustomUserRole
    fk_name = 'user'
    extra = 1
    fields = ('role', 'principal', 'actif', 'compte_id', 'assigne_par')
    autocomplete_fields = ['role', 'assigne_par']


# --- ADMIN CLASSES ---

@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('libelle', 'slug', 'niveau')
    search_fields = ('libelle', 'slug')
    # filter_horizontal permet d'avoir l'interface de sélection
    # des permissions beaucoup plus intuitive (deux colonnes)
    filter_horizontal = ('permissions',)
    ordering = ('niveau',)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('nom_complet', 'email', 'compte_id', 'is_active', 'is_staff', 'get_role_principal')
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'compte_id')
    search_fields = ('nom_complet', 'email', 'keycloak_id')

    # On rend le champ de recherche trgm en lecture seule pour éviter les erreurs
    readonly_fields = ('nom_complet_search', 'created_at', 'updated_at')

    inlines = [CustomUserRoleInline]

    fieldsets = (
        ("Identité Keycloak", {
            'fields': ('keycloak_id', 'compte_id')
        }),
        ("Informations Personnelles", {
            'fields': ('nom_complet', 'nom_complet_search', 'email')
        }),
        ("Permissions & Statuts", {
            'fields': ('is_active', 'is_staff', 'is_superuser')
        }),
        ("Dates", {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_role_principal(self, obj):
        return obj.role_principal

    get_role_principal.short_description = 'Rôle Principal'


@admin.register(CustomUserRole)
class CustomUserRoleAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'compte_id', 'principal', 'actif')
    list_filter = ('actif', 'principal', 'compte_id')
    search_fields = ('user__nom_complet', 'role__libelle')
    autocomplete_fields = ['user', 'role', 'assigne_par']


# Optionnel : Permet de gérer les permissions Django directement si besoin
@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ('name', 'content_type', 'codename')
    search_fields = ('name', 'codename')