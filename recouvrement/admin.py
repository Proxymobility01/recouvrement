from django.contrib import admin
from .models import (
    TypeContrat,
    Contrat,
    SessionPaiement,
    Lease,
    Paiement,
    Parametre,
    ReglePenalite,
    Penalite
)


# ==========================================
# 1. ASTUCE : MODÈLE PROXY POUR LE RENOMMAGE
# ==========================================
class Transaction(SessionPaiement):
    """
    Modèle Proxy : Ne crée aucune table en base de données.
    Sert uniquement à renommer "SessionPaiement" en "Transaction" dans le panel Admin.
    """

    class Meta:
        proxy = True
        verbose_name = "Transaction"
        verbose_name_plural = "Transactions"


# ==========================================
# 2. CONFIGURATION DES ADMINS
# ==========================================

@admin.register(TypeContrat)
class TypeContratAdmin(admin.ModelAdmin):
    list_display = ('libelle', 'code', 'est_principal', 'compte_id', 'created_at')
    list_filter = ('est_principal', 'compte_id')
    search_fields = ('libelle', 'code')
    ordering = ('libelle',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Contrat)
class ContratAdmin(admin.ModelAdmin):
    list_display = ('reference', 'nom_complet', 'type_contrat', 'statut', 'montant_total', 'montant_restant',
                    'compte_id')
    list_filter = ('statut', 'frequence', 'type_contrat', 'compte_id', 'regle_penalite')
    search_fields = ('reference', 'nom_complet', 'immatriculation', 'vin', 'chauffeur__email')

    # 🚀 raw_id_fields : Indispensable pour ne pas faire crasher la page s'il y a 10 000 chauffeurs
    raw_id_fields = ('chauffeur', 'enregistre_par', 'parent', 'regle_penalite')

    # On bloque la modification manuelle des champs générés/calculés
    readonly_fields = ('reference', 'nom_complet_search', 'montant_paye', 'created_at', 'updated_at')
    ordering = ('-created_at',)

    fieldsets = (
        ('Informations Générales', {
            'fields': ('compte_id', 'reference', 'type_contrat', 'parent', 'statut')
        }),
        ('Acteurs', {
            'fields': ('chauffeur', 'nom_complet', 'nom_complet_search', 'enregistre_par')
        }),
        ('Véhicule / Objet', {
            'fields': ('immatriculation', 'vin', 'specificites')
        }),
        ('Finances & Échéancier', {
            'fields': ('montant_total', 'montant_restant', 'montant_paye', 'montant_par_paiement', 'frequence',
                       'regle_penalite')
        }),
        ('Dates', {
            'fields': ('date_debut', 'date_fin', 'prochaine_echeance', 'created_at', 'updated_at')
        }),
    )


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """
    C'est ici qu'on gère l'affichage de SessionPaiement sous le nom "Transaction".
    """
    list_display = ('reference', 'montant_total', 'statut', 'telephone', 'date_validation', 'compte_id')
    list_filter = ('statut', 'compte_id', 'date_validation')
    search_fields = ('reference', 'gateway_reference', 'telephone', 'utilisateur__email')
    raw_id_fields = ('utilisateur',)

    # Personne ne doit pouvoir modifier un payload d'audit ou une date de validation de passerelle
    readonly_fields = ('reference', 'gateway_reference', 'webhook_payload', 'date_validation', 'created_at',
                       'updated_at')
    ordering = ('-created_at',)


@admin.register(Lease)
class LeaseAdmin(admin.ModelAdmin):
    list_display = ('contrat', 'date_echeance', 'montant_attendu', 'montant_paye', 'statut', 'compte_id')
    list_filter = ('statut', 'compte_id', 'date_echeance')
    search_fields = ('contrat__reference', 'nom_complet', 'nom_complet_search')
    raw_id_fields = ('contrat',)
    readonly_fields = ('nom_complet', 'nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-date_echeance',)


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ('montant', 'methode', 'statut', 'date_paiement', 'compte_id')
    list_filter = ('statut', 'methode', 'est_annule', 'compte_id')
    search_fields = ('nom_complet', 'session__reference')
    raw_id_fields = ('contrat', 'lease', 'enregistre_par', 'session')
    readonly_fields = ('nom_complet', 'nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-date_paiement',)


@admin.register(Parametre)
class ParametreAdmin(admin.ModelAdmin):
    list_display = ('compte_id', 'jours_repos_display', 'updated_at')
    list_filter = ('compte_id',)
    search_fields = ('compte_id',)
    readonly_fields = ('created_at', 'updated_at')

    def jours_repos_display(self, obj):
        """Affiche les jours de repos de façon plus lisible qu'un simple JSON."""
        jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        try:
            return ", ".join([jours[i] for i in obj.jours_repos])
        except (IndexError, TypeError):
            return str(obj.jours_repos)

    jours_repos_display.short_description = 'Jours de repos'


# ==========================================
# 3. NOUVEAUX ADMINS : PÉNALITÉS
# ==========================================

@admin.register(ReglePenalite)
class ReglePenaliteAdmin(admin.ModelAdmin):
    list_display = ('nom', 'montant', 'frequence', 'occurrences', 'debut', 'compte_id')
    list_filter = ('frequence', 'compte_id')
    search_fields = ('nom', 'nom_search')
    readonly_fields = ('nom_search', 'created_at', 'updated_at')
    ordering = ('-created_at',)

    fieldsets = (
        ('Configuration Principale', {
            'fields': ('compte_id', 'nom', 'nom_search')
        }),
        ('Paramètres Financiers', {
            'fields': ('montant', 'occurrences')
        }),
        ('Planification', {
            'fields': ('frequence', 'cron_expression', 'debut')
        }),
        ('Dates Système', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(Penalite)
class PenaliteAdmin(admin.ModelAdmin):
    list_display = ('nom_complet', 'montant', 'statut', 'date_application', 'lease', 'compte_id')
    list_filter = ('statut', 'compte_id', 'date_application')

    # On permet la recherche sur le nom, le motif et la référence du contrat lié au lease
    search_fields = ('nom_complet', 'nom_complet_search', 'motif', 'lease__contrat__reference')

    # raw_id_fields indispensable car il peut y avoir des milliers d'échéances
    raw_id_fields = ('lease',)

    readonly_fields = ('nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-date_application',)

    fieldsets = (
        ('Liaison', {
            'fields': ('compte_id', 'lease', 'nom_complet', 'nom_complet_search')
        }),
        ('Détails de la Sanction', {
            'fields': ('montant', 'statut', 'motif', 'date_application')
        }),
        ('Dates Système', {
            'fields': ('created_at', 'updated_at')
        }),
    )