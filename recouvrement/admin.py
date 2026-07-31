import datetime

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.db.models import Count
from django.shortcuts import render
from django.utils import timezone
from django.utils.html import format_html
from django_q.models import Schedule
from croniter import croniter
from rangefilter.filters import DateRangeFilter, DateRangeQuickSelectListFilter
from .services import annuler_leases_et_prolonger, AnnulationLeaseError
from .models import (
    TypeContrat,
    Contrat,
    SessionPaiement,
    Lease,
    Paiement,
    Parametre,
    ReglePenalite,
    Penalite,
    RegleGenerationLease,
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


class DateRangeAvecHierFilter(DateRangeQuickSelectListFilter, DateRangeFilter):
    """
    On conserve le DateRangeFilter (sélecteur de plage personnalisée « du / au »)
    et on lui ajoute des liens rapides : Toutes les dates, Aujourd'hui, Hier,
    Les 7 derniers jours, Ce mois-ci, Cette année.
    (DateRangeQuickSelectListFilter hérite déjà de DateRangeFilter : la plage reste intacte.)
    """

    def __init__(self, field, request, params, model, model_admin, field_path):
        super().__init__(field, request, params, model, model_admin, field_path)

        now = timezone.now()
        if timezone.is_aware(now):
            now = timezone.localtime(now)
        hier = (now - datetime.timedelta(days=1)).date()

        links = list(self.links)
        links.insert(2, ('Hier', {
            self.lookup_kwarg_gte: hier,
            self.lookup_kwarg_lte: hier,
        }))
        self.links = tuple(links)


class AnnulerLeasesForm(forms.Form):
    """Formulaire de la page intermédiaire de l'action « Annuler les échéances »."""

    jours_a_prolonger = forms.IntegerField(
        label="Nombre de jours à prolonger",
        min_value=0,
        initial=0,
        help_text="Jours OUVRÉS ajoutés à la date de fin du contrat. "
                  "Les jours de repos configurés pour l'entreprise sont automatiquement sautés. "
                  "Mettre 0 pour annuler sans prolonger."
    )


class ContratAdminForm(forms.ModelForm):
    class Meta:
        model = Contrat
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        compte_id = cleaned_data.get('compte_id')
        regle_generation = cleaned_data.get('regle_generation')

        if (
            compte_id is not None
            and regle_generation is not None
            and regle_generation.compte_id != compte_id
        ):
            self.add_error(
                'regle_generation',
                "La règle de génération doit appartenir au même compte "
                "que le contrat.",
            )

        return cleaned_data


class RegleGenerationLeaseAdminForm(forms.ModelForm):
    class Meta:
        model = RegleGenerationLease
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        compte_id = cleaned_data.get('compte_id')
        nom = (cleaned_data.get('nom') or '').strip()
        frequence = cleaned_data.get('frequence')
        cron_expression = (cleaned_data.get('cron_expression') or '').strip()

        if compte_id is not None and nom:
            regles_du_compte = RegleGenerationLease.objects.filter(
                compte_id=compte_id,
                nom__iexact=nom,
            )
            if self.instance.pk:
                regles_du_compte = regles_du_compte.exclude(
                    pk=self.instance.pk
                )
            if regles_du_compte.exists():
                self.add_error(
                    'nom',
                    "Une règle portant ce nom existe déjà pour ce compte.",
                )

        if frequence == Schedule.CRON:
            if not cron_expression:
                self.add_error(
                    'cron_expression',
                    "L'expression CRON est obligatoire pour cette fréquence.",
                )
            elif (
                len(cron_expression.split()) != 5
                or not croniter.is_valid(cron_expression)
            ):
                self.add_error(
                    'cron_expression',
                    "Expression CRON invalide. Utilisez cinq champs, par "
                    "exemple : 0 12,22 * * *.",
                )
            else:
                cleaned_data['cron_expression'] = cron_expression
        else:
            cleaned_data['cron_expression'] = None

        return cleaned_data


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
    form = ContratAdminForm
    list_display = (
        'id_reference',
        'nom_complet',
        'type_contrat',
        'statut',
        'regle_generation',
        'montant_total',
        'montant_paye',
        'montant_restant',
        'date_debut',
        'date_fin',
        'prochaine_echeance',
        'created_at',
        'compte_id',
    )
    list_select_related = (
        'type_contrat',
        'regle_generation',
        'regle_penalite',
    )
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('prochaine_echeance', DateRangeAvecHierFilter),
        'statut',
        'frequence',
        'type_contrat',
        'compte_id',
        'regle_generation',
        'regle_penalite',
    )
    search_fields = ('reference', 'nom_complet', 'immatriculation', 'vin', 'chauffeur__email')
    date_hierarchy = 'created_at'

    # 🚀 raw_id_fields : Indispensable pour ne pas faire crasher la page s'il y a 10 000 chauffeurs
    raw_id_fields = (
        'chauffeur',
        'enregistre_par',
        'parent',
        'regle_generation',
        'regle_penalite',
    )

    # On bloque la modification manuelle des champs générés/calculés
    readonly_fields = ('reference', 'nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-created_at',)

    def id_reference(self, obj):
        """Colonne combinée : ID en gras, référence en dessous en plus discret."""
        return format_html('<strong>#{}</strong><br><span style="color:#888;">{}</span>', obj.id, obj.reference)

    id_reference.short_description = 'ID / Référence'
    id_reference.admin_order_field = 'id'

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
                       'regle_generation', 'regle_penalite')
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
    list_display = ('reference', 'montant_total', 'statut', 'telephone', 'date_validation', 'created_at', 'compte_id')
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_validation', DateRangeAvecHierFilter),
        'statut', 'compte_id',
    )
    search_fields = ('reference', 'gateway_reference', 'telephone', 'utilisateur__email')
    raw_id_fields = ('utilisateur',)

    # Personne ne doit pouvoir modifier un payload d'audit ou une date de validation de passerelle
    readonly_fields = ('reference', 'gateway_reference', 'webhook_payload', 'date_validation', 'created_at',
                       'updated_at')
    ordering = ('-created_at',)


@admin.register(Lease)
class LeaseAdmin(admin.ModelAdmin):
    list_display = ('contrat', 'date_echeance', 'created_at', 'montant_attendu', 'montant_paye', 'statut', 'id_compte')
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_echeance', DateRangeAvecHierFilter),
        # Traversée de relation : Lease -> contrat -> type_contrat
        'statut', 'contrat__type_contrat', 'compte_id',
    )
    # Évite le N+1 : la colonne 'contrat' appelle le __str__ du contrat sur chaque ligne
    list_select_related = ('contrat',)
    search_fields = ('contrat__reference', 'nom_complet', 'nom_complet_search')
    raw_id_fields = ('contrat',)
    readonly_fields = ('nom_complet', 'nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-date_echeance',)

    def id_compte(self, obj):
        """Colonne combinée : ID du lease en gras, compte_id (tenant) en dessous."""
        return format_html('<strong>#{}</strong><br><span style="color:#888;">Compte {}</span>', obj.id, obj.compte_id)

    id_compte.short_description = 'ID / Compte'
    id_compte.admin_order_field = 'id'

    actions = ['annuler_et_prolonger']

    @admin.action(description="Annuler les échéances et prolonger le contrat")
    def annuler_et_prolonger(self, request, queryset):
        """
        Action de masse : annule les échéances sélectionnées et prolonge la date de fin
        des contrats concernés. Passe par une page intermédiaire pour saisir le nombre
        de jours, comme le fait la suppression groupée de Django.
        """
        # 2e passage : l'utilisateur a validé la page intermédiaire
        if 'appliquer' in request.POST:
            form = AnnulerLeasesForm(request.POST)

            if form.is_valid():
                jours = form.cleaned_data['jours_a_prolonger']
                try:
                    resultat = annuler_leases_et_prolonger(queryset, jours)
                except AnnulationLeaseError as e:
                    self.message_user(request, str(e), level=messages.ERROR)
                    return None

                message = (
                    f"{resultat['nb_leases']} échéance(s) annulée(s) "
                    f"sur {resultat['nb_contrats']} contrat(s)."
                )
                if resultat['contrats_impactes']:
                    message += (
                        f" Prolongation de {jours} jour(s) ouvré(s) appliquée à : "
                        f"{', '.join(resultat['contrats_impactes'])}."
                    )
                self.message_user(request, message, level=messages.SUCCESS)
                return None  # retour à la liste

        # 1er passage : on affiche la page de confirmation
        else:
            form = AnnulerLeasesForm()

        return render(request, 'admin/recouvrement/annuler_leases.html', {
            **self.admin_site.each_context(request),
            'title': "Annuler les échéances et prolonger le contrat",
            'leases': queryset.select_related('contrat'),
            'form': form,
            'action_checkbox_name': ACTION_CHECKBOX_NAME,
            'selection': queryset.values_list('pk', flat=True),
            'opts': self.model._meta,
        })


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ('contrat', 'lease', 'enregistre_par','nom_complet', 'session', 'methode', 'statut', 'date_paiement',
                    'created_at')
    # Évite le N+1 queries : chaque colonne ci-dessus appelle le __str__ d'une FK différente
    list_select_related = ('contrat', 'lease', 'enregistre_par', 'session')
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_paiement', DateRangeAvecHierFilter),
        'statut', 'methode', 'est_annule', 'compte_id',
    )
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


@admin.register(RegleGenerationLease)
class RegleGenerationLeaseAdmin(admin.ModelAdmin):
    form = RegleGenerationLeaseAdminForm
    list_display = (
        'nom',
        'compte_id',
        'frequence',
        'cron_expression',
        'debut',
        'actif',
        'nombre_contrats',
        'created_at',
    )
    list_filter = (
        'actif',
        'frequence',
        'compte_id',
        ('debut', DateRangeAvecHierFilter),
        ('created_at', DateRangeAvecHierFilter),
    )
    search_fields = (
        'nom',
        'nom_search',
    )
    readonly_fields = (
        'nom_search',
        'created_at',
        'updated_at',
    )
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'

    fieldsets = (
        ('Identification', {
            'fields': ('compte_id', 'nom', 'nom_search', 'actif')
        }),
        ('Planification', {
            'fields': ('frequence', 'cron_expression', 'debut')
        }),
        ('Dates système', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_nombre_contrats=Count('contrats'))
        )

    @admin.display(
        description='Contrats',
        ordering='_nombre_contrats',
    )
    def nombre_contrats(self, obj):
        return obj._nombre_contrats


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
