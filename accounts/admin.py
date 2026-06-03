from django.contrib import admin
from .models import Role, CustomUser, CustomUserRole, ConfigPaiement


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('libelle', 'slug', 'niveau')
    search_fields = ('libelle', 'slug')
    list_filter = ('niveau',)
    prepopulated_fields = {'slug': ('libelle',)}
    filter_horizontal = ('permissions',)
    ordering = ('-niveau', 'libelle')


class CustomUserRoleInline(admin.TabularInline):
    """
    Permet d'affecter des rôles directement depuis la fiche d'un utilisateur.
    """
    model = CustomUserRole
    fk_name = 'user'
    extra = 0
    fields = ('role', 'compte_id', 'principal', 'actif', 'assigne_par')
    raw_id_fields = ('assigne_par',)  # Affiche un champ de recherche au lieu d'une liste déroulante géante


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('nom_complet', 'keycloak_id', 'email', 'compte_id', 'is_active', 'is_staff',
                    'role_principal_display')
    search_fields = ('nom_complet', 'keycloak_id', 'email', 'compte_id')
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'compte_id')
    readonly_fields = ('created_at', 'updated_at', 'nom_complet_search')
    inlines = [CustomUserRoleInline]
    ordering = ('-created_at',)

    fieldsets = (
        ('Identité (Keycloak)', {
            'fields': ('keycloak_id', 'compte_id', 'nom_complet', 'nom_complet_search', 'email')
        }),
        ('Permissions & Statut', {
            'fields': ('is_active', 'is_staff', 'is_superuser')
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def role_principal_display(self, obj):
        """
        Affiche le rôle principal directement dans la liste des utilisateurs.
        """
        role = obj.role_principal
        return role.libelle if role else "—"

    role_principal_display.short_description = "Rôle Principal"


@admin.register(CustomUserRole)
class CustomUserRoleAdmin(admin.ModelAdmin):
    """
    Vue globale de toutes les affectations de rôles.
    Très utile pour chercher "Qui est admin dans le compte 5 ?".
    """
    list_display = ('user', 'role', 'compte_id', 'principal', 'actif', 'created_at')
    search_fields = ('user__nom_complet', 'user__keycloak_id', 'role__libelle')
    list_filter = ('actif', 'principal', 'role', 'compte_id')
    raw_id_fields = ('user', 'assigne_par')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)


@admin.register(ConfigPaiement)
class ConfigPaiementAdmin(admin.ModelAdmin):
    list_display = ('compte_id', 'base_url', 'created_at', 'updated_at')
    search_fields = ('compte_id', 'base_url')
    list_filter = ('created_at',)
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Informations Générales', {
            'fields': ('compte_id',)
        }),
        ('Configuration API (PayGate)', {
            'fields': ('api_key', 'base_url', 'success_url')
        }),
        ('Sécurité Webhook', {
            'fields': ('webhook_secret',)
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )