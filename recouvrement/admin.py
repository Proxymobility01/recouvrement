import datetime

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.db import transaction
from django.db.models import Count
from django.forms.models import BaseInlineFormSet
from django.shortcuts import render
from django.utils import timezone
from django_q.models import Schedule
from croniter import croniter
from rangefilter.filters import DateRangeFilter, DateRangeQuickSelectListFilter
from accounts.models import ConfigPaiement
from core.admin_mixins import IdCompteAdminMixin
from core.exceptions import CustomAPIException
from .services import annuler_leases_et_prolonger, AnnulationLeaseError
from .services_ussd import PaiementUSSDService
from .models import (
    Agence,
    Proprietaire,
    CompteReceptionProprietaire,
    TypeContrat,
    Contrat,
    SessionPaiement,
    PreuvePaiementUSSD,
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


class AssignerRegleGenerationContratsForm(forms.Form):
    regle_generation = forms.ModelChoiceField(
        label="Règle de génération de lease",
        queryset=RegleGenerationLease.objects.none(),
        help_text=(
            "Seules les règles appartenant au compte des contrats "
            "sélectionnés sont proposées. Une règle inactive peut être "
            "attribuée, mais elle ne générera aucun lease tant qu'elle "
            "restera inactive."
        ),
    )

    def __init__(self, *args, compte_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        if compte_id is not None:
            self.fields['regle_generation'].queryset = (
                RegleGenerationLease.objects
                .filter(compte_id=compte_id)
                .order_by('-actif', 'nom')
            )
        self.fields['regle_generation'].label_from_instance = (
            lambda regle: (
                f"{regle.nom} — {regle.get_frequence_display()}"
                f"{'' if regle.actif else ' (inactive)'}"
            )
        )


class AssignerConfigPaiementContratsForm(forms.Form):
    config_paiement = forms.ModelChoiceField(
        label="Configuration de paiement",
        queryset=ConfigPaiement.objects.none(),
        required=False,
        empty_label="Aucune configuration Mobile Money",
        help_text=(
            "Les contrats sans configuration restent payables par les "
            "autres moyens, mais pas par Mobile Money."
        ),
    )

    def __init__(self, *args, compte_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        if compte_id is not None:
            self.fields['config_paiement'].queryset = (
                ConfigPaiement.objects
                .filter(compte_id=compte_id)
                .order_by('-actif', 'nom')
            )


class ContratAdminForm(forms.ModelForm):
    class Meta:
        model = Contrat
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        compte_id = cleaned_data.get('compte_id')
        agence = cleaned_data.get('agence')
        parent = cleaned_data.get('parent')
        proprietaire = cleaned_data.get('proprietaire')
        regle_generation = cleaned_data.get('regle_generation')
        config_paiement = cleaned_data.get('config_paiement')

        if (
            compte_id is not None
            and agence is not None
            and agence.compte_id != compte_id
        ):
            self.add_error(
                'agence',
                "L'agence doit appartenir au même compte que le contrat.",
            )

        if (
            agence is not None
            and not agence.actif
            and self.instance.agence_id != agence.id
        ):
            self.add_error(
                'agence',
                "Une agence inactive ne peut pas recevoir un contrat.",
            )

        if parent is not None:
            if agence is None:
                agence = parent.agence
                cleaned_data['agence'] = agence
            elif parent.agence_id != agence.id:
                self.add_error(
                    'agence',
                    "Un sous-contrat doit appartenir à l'agence de son parent.",
                )

            # La configuration de paiement est pilotée exclusivement par le
            # contrat principal. À la création, toute valeur saisie sur le
            # sous-contrat est donc remplacée par celle du parent.
            config_paiement = parent.config_paiement
            cleaned_data['config_paiement'] = config_paiement

            proprietaire = parent.proprietaire
            cleaned_data['proprietaire'] = proprietaire
        elif proprietaire is None and not self.instance.pk:
            self.add_error(
                'proprietaire',
                "Le propriétaire du contrat principal est obligatoire.",
            )

        if (
            compte_id is not None
            and proprietaire is not None
            and proprietaire.compte_id != compte_id
        ):
            self.add_error(
                'proprietaire',
                "Le propriétaire doit appartenir au même compte que le "
                "contrat.",
            )

        if (
            proprietaire is not None
            and not proprietaire.actif
            and self.instance.proprietaire_id != proprietaire.id
        ):
            self.add_error(
                'proprietaire',
                "Un propriétaire inactif ne peut pas recevoir un contrat.",
            )

        proprietaire_actuel_id = getattr(
            self.instance,
            'proprietaire_id',
            None,
        )
        proprietaire_demande_id = (
            proprietaire.id if proprietaire is not None else None
        )
        if (
            self.instance.pk
            and self.instance.parent_id is None
            and proprietaire_demande_id != proprietaire_actuel_id
            and self.instance.possede_historique_financier()
        ):
            self.add_error(
                'proprietaire',
                "Le propriétaire ne peut plus être modifié car ce contrat "
                "ou l'un de ses sous-contrats possède déjà un historique "
                "financier.",
            )

        agence_actuelle_id = getattr(self.instance, 'agence_id', None)
        agence_demandee_id = agence.id if agence is not None else None
        if (
            self.instance.pk
            and agence_demandee_id != agence_actuelle_id
            and (
                self.instance.leases.exists()
                or self.instance.paiements.exists()
                or self.instance.sous_contrats.filter(
                    leases__isnull=False,
                ).exists()
                or self.instance.sous_contrats.filter(
                    paiements__isnull=False,
                ).exists()
            )
        ):
            self.add_error(
                'agence',
                "L'agence ne peut plus être modifiée car ce contrat ou l'un "
                "de ses sous-contrats possède déjà un historique financier.",
            )

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

        if (
            compte_id is not None
            and config_paiement is not None
            and config_paiement.compte_id != compte_id
        ):
            self.add_error(
                'config_paiement',
                "La configuration de paiement doit appartenir au même "
                "compte que le contrat.",
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

@admin.register(Agence)
class AgenceAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte',
        'code',
        'nom',
        'zone',
        'actif',
        'created_at',
    )
    list_filter = ('zone', 'actif', 'compte_id')
    search_fields = (
        'code', 'nom_search', 'zone_search', 'adresse', 'telephone', 'email',
    )
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('compte_id', 'nom')
    fieldsets = (
        ('Identification', {
            'fields': ('compte_id', 'code', 'nom', 'zone', 'actif')
        }),
        ('Coordonnées', {
            'fields': ('adresse', 'telephone', 'email')
        }),
        ('Dates système', {
            'fields': ('created_at', 'updated_at')
        }),
    )


class RejeterPreuvesUSSDForm(forms.Form):
    motif = forms.CharField(
        label='Motif du rejet',
        widget=forms.Textarea(attrs={'rows': 4, 'cols': 70}),
        help_text=(
            "Ce motif sera conservé dans l'audit de chaque preuve "
            "sélectionnée."
        ),
    )


class CompteReceptionProprietaireInlineFormSet(BaseInlineFormSet):
    def save_new(self, form, commit=True):
        objet = form.save(commit=False)
        objet.proprietaire = self.instance
        objet.compte_id = self.instance.compte_id
        if commit:
            objet.save()
            form.save_m2m()
        return objet


class CompteReceptionProprietaireInline(admin.TabularInline):
    model = CompteReceptionProprietaire
    formset = CompteReceptionProprietaireInlineFormSet
    extra = 0
    fields = ('operateur', 'numero', 'nom_titulaire', 'actif')


@admin.register(Proprietaire)
class ProprietaireAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'nom_complet', 'actif', 'created_at',
    )
    list_filter = ('actif', 'compte_id')
    search_fields = (
        'nom_complet_search', 'comptes_reception__numero',
    )
    readonly_fields = (
        'nom_complet_search', 'created_at', 'updated_at',
    )
    ordering = ('nom_complet',)
    inlines = (CompteReceptionProprietaireInline,)


@admin.register(CompteReceptionProprietaire)
class CompteReceptionProprietaireAdmin(
    IdCompteAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        'id_compte', 'proprietaire', 'operateur', 'numero',
        'nom_titulaire', 'actif', 'created_at',
    )
    list_select_related = ('proprietaire',)
    list_filter = ('operateur', 'actif', 'compte_id')
    search_fields = (
        'numero', 'nom_titulaire_search',
        'proprietaire__nom_complet_search',
    )
    raw_id_fields = ('proprietaire',)
    readonly_fields = (
        'nom_titulaire_search', 'created_at', 'updated_at',
    )
    ordering = ('proprietaire__nom_complet', 'operateur')


@admin.register(TypeContrat)
class TypeContratAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'libelle', 'code', 'est_principal', 'created_at',
    )
    list_filter = ('est_principal', 'compte_id')
    search_fields = ('libelle', 'code')
    ordering = ('libelle',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Contrat)
class ContratAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    form = ContratAdminForm
    actions = [
        'assigner_regle_generation',
        'assigner_config_paiement',
    ]
    list_display = (
        'id_compte',
        'reference',
        'nom_complet',
        'type_contrat',
        'statut',
        'agence',
        'proprietaire',
        'regle_generation',
        'config_paiement',
        'montant_total',
        'montant_paye',
        'montant_restant',
        'date_debut',
        'date_fin',
        'prochaine_echeance',
        'created_at',
    )
    list_select_related = (
        'type_contrat',
        'agence',
        'proprietaire',
        'regle_generation',
        'regle_penalite',
        'config_paiement',
    )
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('prochaine_echeance', DateRangeAvecHierFilter),
        'statut',
        'frequence',
        'type_contrat',
        'compte_id',
        'agence',
        'proprietaire',
        'regle_generation',
        'config_paiement',
        'regle_penalite',
    )
    search_fields = (
        'reference',
        'nom_complet',
        'immatriculation',
        'vin',
        'chauffeur__email',
        'agence__code',
        'agence__nom_search',
        'agence__zone_search',
        'proprietaire__nom_complet_search',
        'proprietaire__comptes_reception__numero',
    )
    date_hierarchy = 'created_at'

    # 🚀 raw_id_fields : Indispensable pour ne pas faire crasher la page s'il y a 10 000 chauffeurs
    raw_id_fields = (
        'chauffeur',
        'enregistre_par',
        'parent',
        'agence',
        'proprietaire',
        'regle_generation',
        'config_paiement',
        'regle_penalite',
    )

    # On bloque la modification manuelle des champs générés/calculés
    readonly_fields = ('reference', 'nom_complet_search', 'created_at', 'updated_at')
    ordering = ('-created_at',)

    def get_readonly_fields(self, request, obj=None):
        champs = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.parent_id is not None:
            champs.append('config_paiement')
            champs.append('proprietaire')
        return tuple(champs)

    def save_model(self, request, obj, form, change):
        ancienne_agence_id = None
        if change and obj.pk:
            ancienne_agence_id = (
                Contrat.objects
                .filter(pk=obj.pk)
                .values_list('agence_id', flat=True)
                .first()
            )

        super().save_model(request, obj, form, change)

        if (
            change
            and obj.parent_id is None
            and ancienne_agence_id != obj.agence_id
        ):
            obj.sous_contrats.update(
                agence_id=obj.agence_id,
                updated_at=timezone.now(),
            )

    fieldsets = (
        ('Informations Générales', {
            'fields': (
                'compte_id', 'agence', 'reference', 'type_contrat',
                'parent', 'proprietaire', 'statut',
            )
        }),
        ('Acteurs', {
            'fields': ('chauffeur', 'nom_complet', 'nom_complet_search', 'enregistre_par')
        }),
        ('Véhicule / Objet', {
            'fields': ('immatriculation', 'vin', 'specificites')
        }),
        ('Finances & Échéancier', {
            'fields': ('montant_total', 'montant_restant', 'montant_paye', 'montant_par_paiement', 'frequence',
                       'regle_generation', 'regle_penalite', 'config_paiement')
        }),
        ('Dates', {
            'fields': ('date_debut', 'date_fin', 'prochaine_echeance', 'created_at', 'updated_at')
        }),
    )

    @admin.action(
        description="Attribuer une règle de génération aux contrats sélectionnés"
    )
    def assigner_regle_generation(self, request, queryset):
        compte_ids = list(
            queryset
            .order_by()
            .values_list('compte_id', flat=True)
            .distinct()[:2]
        )

        if len(compte_ids) != 1:
            self.message_user(
                request,
                "Sélectionnez uniquement des contrats appartenant au même "
                "compte avant d'attribuer une règle de génération.",
                level=messages.ERROR,
            )
            return None

        contrats_non_actifs = queryset.exclude(
            statut__in=Contrat.STATUTS_ASSIGNABLES_REGLE_GENERATION,
        ).count()
        if contrats_non_actifs:
            self.message_user(
                request,
                "L'attribution d'une règle de génération est réservée aux "
                f"contrats actifs. La sélection contient "
                f"{contrats_non_actifs} contrat(s) non actif(s).",
                level=messages.ERROR,
            )
            return None

        compte_id = compte_ids[0]

        if 'appliquer' in request.POST:
            form = AssignerRegleGenerationContratsForm(
                request.POST,
                compte_id=compte_id,
            )
            if form.is_valid():
                regle = form.cleaned_data['regle_generation']
                contrats_modifies = queryset.exclude(
                    regle_generation_id=regle.id,
                ).update(
                    regle_generation=regle,
                    updated_at=timezone.now(),
                )
                self.message_user(
                    request,
                    f"La règle « {regle.nom} » a été attribuée à "
                    f"{contrats_modifies} contrat(s). La prochaine échéance "
                    "de ces contrats n'a pas été modifiée.",
                    level=messages.SUCCESS,
                )
                return None
        else:
            form = AssignerRegleGenerationContratsForm(
                compte_id=compte_id,
            )

        return render(
            request,
            'admin/recouvrement/assigner_regle_generation.html',
            {
                **self.admin_site.each_context(request),
                'title': (
                    "Attribuer une règle de génération de lease aux contrats"
                ),
                'contrats': queryset.select_related(
                    'type_contrat',
                    'regle_generation',
                ),
                'form': form,
                'action_checkbox_name': ACTION_CHECKBOX_NAME,
                'selection': queryset.values_list('pk', flat=True),
                'opts': self.model._meta,
            },
        )

    @admin.action(
        description=(
            "Attribuer une configuration de paiement aux contrats principaux sélectionnés"
        )
    )
    def assigner_config_paiement(self, request, queryset):
        compte_ids = list(
            queryset
            .order_by()
            .values_list('compte_id', flat=True)
            .distinct()[:2]
        )

        if len(compte_ids) != 1:
            self.message_user(
                request,
                "Sélectionnez uniquement des contrats appartenant au même "
                "compte avant d'attribuer une configuration de paiement.",
                level=messages.ERROR,
            )
            return None

        sous_contrats_selectionnes = queryset.filter(
            parent__isnull=False,
        ).count()
        if sous_contrats_selectionnes:
            self.message_user(
                request,
                "La configuration de paiement se gère uniquement depuis "
                "les contrats principaux. Retirez les sous-contrats de la "
                "sélection.",
                level=messages.ERROR,
            )
            return None

        compte_id = compte_ids[0]
        if 'appliquer_config_paiement' in request.POST:
            form = AssignerConfigPaiementContratsForm(
                request.POST,
                compte_id=compte_id,
            )
            if form.is_valid():
                config = form.cleaned_data['config_paiement']
                parent_ids = list(queryset.values_list('pk', flat=True))
                maintenant = timezone.now()
                with transaction.atomic():
                    contrats_modifies = queryset.update(
                        config_paiement=config,
                        updated_at=maintenant,
                    )
                    sous_contrats_modifies = Contrat.objects.filter(
                        parent_id__in=parent_ids,
                    ).update(
                        config_paiement=config,
                        updated_at=maintenant,
                    )
                destination = (
                    f"la configuration « {config.nom} »"
                    if config
                    else "aucune configuration Mobile Money"
                )
                self.message_user(
                    request,
                    f"{contrats_modifies} contrat(s) principal(aux) et "
                    f"{sous_contrats_modifies} sous-contrat(s) utiliseront "
                    f"désormais {destination}.",
                    level=messages.SUCCESS,
                )
                return None
        else:
            form = AssignerConfigPaiementContratsForm(
                compte_id=compte_id,
            )

        return render(
            request,
            'admin/recouvrement/assigner_config_paiement.html',
            {
                **self.admin_site.each_context(request),
                'title': (
                    "Attribuer une configuration de paiement aux contrats"
                ),
                'contrats': queryset.select_related('config_paiement'),
                'form': form,
                'action_checkbox_name': ACTION_CHECKBOX_NAME,
                'selection': queryset.values_list('pk', flat=True),
                'opts': self.model._meta,
            },
        )


@admin.register(Transaction)
class TransactionAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    """
    C'est ici qu'on gère l'affichage de SessionPaiement sous le nom "Transaction".
    """
    list_display = (
        'id_compte',
        'reference',
        'montant_total',
        'canal',
        'statut',
        'agence',
        'proprietaire',
        'operateur',
        'numero_destinataire',
        'config_paiement',
        'telephone',
        'date_validation',
        'created_at',
    )
    list_select_related = (
        'agence', 'config_paiement', 'utilisateur', 'proprietaire',
        'compte_reception',
    )
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_validation', DateRangeAvecHierFilter),
        'statut', 'canal', 'operateur', 'compte_id', 'agence',
        'proprietaire',
    )
    search_fields = (
        'reference',
        'gateway_reference',
        'telephone',
        'utilisateur__email',
        'agence__code',
        'agence__nom_search',
        'agence__zone_search',
        'proprietaire__nom_complet_search',
        'numero_destinataire',
    )
    raw_id_fields = ('utilisateur', 'proprietaire', 'compte_reception')

    # Personne ne doit pouvoir modifier un payload d'audit ou une date de validation de passerelle
    readonly_fields = (
        'reference',
        'gateway_reference',
        'agence',
        'config_paiement',
        'proprietaire',
        'compte_reception',
        'operateur',
        'numero_destinataire',
        'nom_destinataire',
        'webhook_payload',
        'date_validation',
        'created_at',
        'updated_at',
    )
    ordering = ('-created_at',)


@admin.register(PreuvePaiementUSSD)
class PreuvePaiementUSSDAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'session', 'statut', 'operateur',
        'reference_operateur', 'montant_transfere', 'frais_operateur',
        'numero_expediteur', 'numero_destinataire', 'created_at',
    )
    list_select_related = ('session', 'verifie_par')
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('verifie_le', DateRangeAvecHierFilter),
        'statut', 'operateur', 'compte_id',
    )
    search_fields = (
        'session__reference', 'reference_operateur', 'numero_expediteur',
        'numero_destinataire', 'nom_expediteur', 'nom_destinataire',
    )
    raw_id_fields = ('session', 'verifie_par')
    readonly_fields = tuple(
        field.name for field in PreuvePaiementUSSD._meta.fields
    )
    ordering = ('-created_at',)
    actions = ('valider_preuves_ussd', 'rejeter_preuves_ussd')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.has_perm(
            'recouvrement.can_validate_ussd_payment'
        ):
            actions.pop('valider_preuves_ussd', None)
            actions.pop('rejeter_preuves_ussd', None)
        return actions

    @admin.action(description='Valider les preuves USSD sélectionnées')
    def valider_preuves_ussd(self, request, queryset):
        validees = 0
        deja_traitees = 0
        erreurs = []

        for preuve in queryset.order_by('id'):
            try:
                _, modifiee = PaiementUSSDService.valider_preuve(
                    preuve_id=preuve.id,
                    agent=request.user,
                )
            except CustomAPIException as exc:
                erreurs.append(f"Preuve #{preuve.id} : {exc.dev_message}")
                continue

            if modifiee:
                validees += 1
            else:
                deja_traitees += 1

        if validees:
            self.message_user(
                request,
                f"{validees} preuve(s) validée(s). La ventilation des "
                "paiements a été déclenchée.",
                level=messages.SUCCESS,
            )
        if deja_traitees:
            self.message_user(
                request,
                f"{deja_traitees} preuve(s) étaient déjà validée(s).",
                level=messages.INFO,
            )
        if erreurs:
            self.message_user(
                request,
                ' | '.join(erreurs),
                level=messages.ERROR,
            )

    @admin.action(description='Rejeter les preuves USSD sélectionnées')
    def rejeter_preuves_ussd(self, request, queryset):
        if 'appliquer_rejet_ussd' in request.POST:
            form = RejeterPreuvesUSSDForm(request.POST)
            if form.is_valid():
                rejetees = 0
                deja_traitees = 0
                erreurs = []

                for preuve in queryset.order_by('id'):
                    try:
                        _, modifiee = PaiementUSSDService.rejeter_preuve(
                            preuve_id=preuve.id,
                            agent=request.user,
                            motif=form.cleaned_data['motif'],
                        )
                    except CustomAPIException as exc:
                        erreurs.append(
                            f"Preuve #{preuve.id} : {exc.dev_message}"
                        )
                        continue

                    if modifiee:
                        rejetees += 1
                    else:
                        deja_traitees += 1

                if rejetees:
                    self.message_user(
                        request,
                        f"{rejetees} preuve(s) rejetée(s). Les échéances "
                        "peuvent être payées à nouveau.",
                        level=messages.SUCCESS,
                    )
                if deja_traitees:
                    self.message_user(
                        request,
                        f"{deja_traitees} preuve(s) étaient déjà rejetée(s).",
                        level=messages.INFO,
                    )
                if erreurs:
                    self.message_user(
                        request,
                        ' | '.join(erreurs),
                        level=messages.ERROR,
                    )
                return None
        else:
            form = RejeterPreuvesUSSDForm()

        return render(
            request,
            'admin/recouvrement/rejeter_preuves_ussd.html',
            {
                **self.admin_site.each_context(request),
                'title': 'Rejeter des preuves de paiement USSD',
                'preuves': queryset.select_related('session'),
                'form': form,
                'action_checkbox_name': ACTION_CHECKBOX_NAME,
                'selection': queryset.values_list('pk', flat=True),
                'opts': self.model._meta,
            },
        )


@admin.register(Lease)
class LeaseAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte',
        'contrat',
        'agence',
        'date_echeance',
        'created_at',
        'montant_attendu',
        'montant_paye',
        'statut',
    )
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_echeance', DateRangeAvecHierFilter),
        # Traversée de relation : Lease -> contrat -> type_contrat
        'statut', 'contrat__type_contrat', 'compte_id', 'agence',
    )
    # Évite le N+1 : la colonne 'contrat' appelle le __str__ du contrat sur chaque ligne
    list_select_related = ('contrat', 'agence')
    search_fields = (
        'contrat__reference',
        'nom_complet',
        'nom_complet_search',
        'agence__code',
        'agence__nom_search',
        'agence__zone_search',
    )
    raw_id_fields = ('contrat',)
    readonly_fields = (
        'agence',
        'nom_complet',
        'nom_complet_search',
        'created_at',
        'updated_at',
    )
    ordering = ('-date_echeance',)

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
class PaiementAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'contrat', 'lease', 'agence', 'enregistre_par',
        'nom_complet', 'session', 'methode', 'statut', 'date_paiement',
        'created_at',
    )
    # Évite le N+1 queries : chaque colonne ci-dessus appelle le __str__ d'une FK différente
    list_select_related = (
        'contrat', 'lease', 'agence', 'enregistre_par', 'session',
    )
    list_filter = (
        ('created_at', DateRangeAvecHierFilter),
        ('date_paiement', DateRangeAvecHierFilter),
        'statut', 'methode', 'est_annule', 'compte_id', 'agence',
    )
    search_fields = (
        'nom_complet',
        'session__reference',
        'agence__code',
        'agence__nom_search',
        'agence__zone_search',
    )
    raw_id_fields = ('contrat', 'lease', 'enregistre_par', 'session')
    readonly_fields = (
        'agence',
        'nom_complet',
        'nom_complet_search',
        'created_at',
        'updated_at',
    )
    ordering = ('-date_paiement',)


@admin.register(Parametre)
class ParametreAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = ('id_compte', 'jours_repos_display', 'updated_at')
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
class ReglePenaliteAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'nom', 'montant', 'frequence', 'occurrences', 'debut',
    )
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
class RegleGenerationLeaseAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    form = RegleGenerationLeaseAdminForm
    list_display = (
        'id_compte',
        'nom',
        'frequence',
        'cron_expression',
        'debut',
        'actif',
        'defaut',
        'nombre_contrats',
        'created_at',
    )
    list_filter = (
        'actif',
        'defaut',
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
            'fields': (
                'compte_id', 'nom', 'nom_search', 'actif', 'defaut',
            )
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
class PenaliteAdmin(IdCompteAdminMixin, admin.ModelAdmin):
    list_display = (
        'id_compte', 'nom_complet', 'montant', 'statut',
        'date_application', 'lease', 'agence',
    )
    list_filter = ('statut', 'compte_id', 'agence', 'date_application')
    list_select_related = ('lease', 'agence')

    # On permet la recherche sur le nom, le motif et la référence du contrat lié au lease
    search_fields = (
        'nom_complet',
        'nom_complet_search',
        'motif',
        'lease__contrat__reference',
        'agence__code',
        'agence__nom_search',
        'agence__zone_search',
    )

    # raw_id_fields indispensable car il peut y avoir des milliers d'échéances
    raw_id_fields = ('lease',)

    readonly_fields = (
        'agence',
        'nom_complet_search',
        'created_at',
        'updated_at',
    )
    ordering = ('-date_application',)

    fieldsets = (
        ('Liaison', {
            'fields': (
                'compte_id', 'agence', 'lease', 'nom_complet',
                'nom_complet_search',
            )
        }),
        ('Détails de la Sanction', {
            'fields': ('montant', 'statut', 'motif', 'date_application')
        }),
        ('Dates Système', {
            'fields': ('created_at', 'updated_at')
        }),
    )
