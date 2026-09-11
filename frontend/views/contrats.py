import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.filters import ContratFilter
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.api.v1.serializers import ContratSerializer, SousContratSerializer
from recouvrement.models import Agence, Contrat, Proprietaire, TypeContrat
from recouvrement.services import AnnulationLeaseError, annuler_leases_et_prolonger

CustomUser = get_user_model()


def _contrats_accessibles(user):
    qs = queryset_tenant(Contrat, user).select_related(
        'chauffeur', 'type_contrat', 'agence', 'proprietaire',
    )
    if user.is_superuser or user.has_perm('recouvrement.view_all_contrats'):
        return qs
    return qs.filter(Q(chauffeur=user) | Q(enregistre_par=user))


def _valeurs_depuis_contrat(contrat):
    """
    Pré-remplit le formulaire d'édition à partir d'un contrat existant.

    Toujours des chaînes simples : le template n'a ainsi jamais besoin de
    retomber sur `contrat.<champ>` en argument de filtre (ex: `|default:
    contrat.x`), ce qui plante si `contrat` est None (cas création) — les
    arguments de filtre ne bénéficient pas de la résolution silencieuse
    dont profite la variable principale d'une expression.
    """
    if contrat is None:
        return {}
    return {
        'chauffeur': str(contrat.chauffeur_id),
        'type_contrat': str(contrat.type_contrat_id or ''),
        'proprietaire': str(contrat.proprietaire_id or ''),
        'agence': str(contrat.agence_id or ''),
        'immatriculation': contrat.immatriculation or '',
        'vin': contrat.vin or '',
        'montant_total': contrat.montant_total,
        'montant_par_paiement': contrat.montant_par_paiement,
        'montant_paye': contrat.montant_paye,
        'frequence': contrat.frequence,
        'date_debut': contrat.date_debut.isoformat() if contrat.date_debut else '',
        'date_fin': contrat.date_fin.isoformat() if contrat.date_fin else '',
        'prochaine_echeance': (
            timezone.localtime(contrat.prochaine_echeance).strftime('%Y-%m-%dT%H:%M')
            if contrat.prochaine_echeance else ''
        ),
        'statut': contrat.statut,
    }


def _lignes_specificites(specificites):
    """
    Dict `specificites` -> liste de tuples (clé, valeur) pour le template.
    Toujours au moins une ligne (vide si rien à afficher), pour que le
    formulaire propose immédiatement une ligne à remplir.
    """
    if specificites:
        return list(specificites.items())
    return [('', '')]


def _lignes_specificites_depuis_post(request):
    """Repropose exactement les lignes clé/valeur telles que soumises (y compris incomplètes)."""
    cles = request.POST.getlist('specificite_cle')
    valeurs = request.POST.getlist('specificite_valeur')
    return list(zip(cles, valeurs)) or [('', '')]


def _specificites_json_depuis_post(request):
    """
    Sérialise les lignes clé/valeur soumises en JSON pour injection dans les
    données envoyées au serializer DRF (les lignes sans clé sont ignorées).
    """
    cles = request.POST.getlist('specificite_cle')
    valeurs = request.POST.getlist('specificite_valeur')
    specificites = {cle.strip(): valeur.strip() for cle, valeur in zip(cles, valeurs) if cle.strip()}
    return json.dumps(specificites)


def _options_formulaire(user):
    compte_id = user.compte_id
    return {
        'chauffeurs': CustomUser.objects.filter(
            compte_id=compte_id, roles__slug='DRIVER', is_active=True,
        ).distinct().order_by('nom_complet'),
        'types_contrat': TypeContrat.objects.filter(
            compte_id=compte_id, est_principal=True,
        ).order_by('libelle'),
        'proprietaires': Proprietaire.objects.filter(
            compte_id=compte_id, actif=True,
        ).order_by('nom_complet'),
        'agences': Agence.objects.filter(
            compte_id=compte_id, actif=True,
        ).order_by('nom'),
    }


@permission_requise('recouvrement.view_contrat')
def liste(request):
    qs = _contrats_accessibles(request.user)

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(
            Q(reference__icontains=recherche)
            | Q(immatriculation__icontains=recherche)
            | Q(vin__icontains=recherche)
            | Q(nom_complet__icontains=recherche)
        )

    # Statut, type de contrat, agence, plages de dates (début/fin/prochaine
    # échéance) et plages de montants : tous déjà supportés par ContratFilter
    # (utilisé aussi par l'API), réutilisé ici tel quel.
    qs = ContratFilter(request.GET, queryset=qs).qs

    qs = qs.annotate(
        montant_total_depenses=Coalesce(
            Sum('depenses__montant'),
            Decimal('0.00'),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )

    page = Paginator(qs.order_by('-created_at'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/contrats/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'filtres': request.GET,
        'statuts': Contrat.STATUT_CHOICES,
        'types_contrat': queryset_tenant(TypeContrat, request.user).order_by('libelle'),
        'agences': queryset_tenant(Agence, request.user).filter(actif=True).order_by('nom'),
        'querystring': querystring_sans_page(request),
    })


@permission_requise('recouvrement.add_contrat')
def creer(request):
    options = _options_formulaire(request.user)
    contexte_commun = {
        'options': options,
        'contrat': None,
        'frequences': Contrat.FREQUENCE_CHOICES,
        'statuts': Contrat.STATUT_CHOICES,
    }

    if request.method != 'POST':
        return render(request, 'frontend/contrats/_modal_form.html', {
            **contexte_commun, 'erreurs': {}, 'valeurs': {},
            'specificites_lignes': _lignes_specificites(None),
        })

    # .copy() : ContratSerializer.create() fait initial_data.pop('sous_contrats', []),
    # ce qui échoue sur un QueryDict immuable (request.POST l'est par défaut).
    donnees = request.POST.copy()
    donnees['specificites'] = _specificites_json_depuis_post(request)
    serializer = ContratSerializer(
        data=donnees, context={'request': request},
    )
    if serializer.is_valid():
        serializer.validated_data['enregistre_par'] = request.user
        try:
            contrat = serializer.save(compte_id=request.user.compte_id)
        except IntegrityError as exc:
            if 'uniq_contrat_parent_actif_par_chauffeur' in str(exc):
                messages.error(request, "Ce chauffeur possède déjà un contrat actif.")
            else:
                messages.error(request, "Erreur d'enregistrement du contrat.")
        except DjangoValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"Contrat {contrat.reference} créé.")
            return redirect('frontend:contrats-detail', pk=contrat.pk)

    return render(request, 'frontend/contrats/_modal_form.html', {
        **contexte_commun, 'erreurs': serializer.errors, 'valeurs': request.POST,
        'specificites_lignes': _lignes_specificites_depuis_post(request),
    })


@permission_requise('recouvrement.change_contrat')
def modifier(request, pk):
    contrat = get_object_or_404(_contrats_accessibles(request.user), pk=pk)
    options = _options_formulaire(request.user)
    contexte_commun = {
        'options': options,
        'contrat': contrat,
        'frequences': Contrat.FREQUENCE_CHOICES,
        'statuts': Contrat.STATUT_CHOICES,
    }

    if request.method != 'POST':
        return render(request, 'frontend/contrats/_modal_form.html', {
            **contexte_commun, 'erreurs': {}, 'valeurs': _valeurs_depuis_contrat(contrat),
            'specificites_lignes': _lignes_specificites(contrat.specificites),
        })

    donnees = request.POST.copy()
    donnees['specificites'] = _specificites_json_depuis_post(request)
    serializer = ContratSerializer(
        contrat, data=donnees, context={'request': request}, partial=True,
    )
    if serializer.is_valid():
        try:
            serializer.save(compte_id=request.user.compte_id)
        except IntegrityError:
            messages.error(request, "Erreur d'enregistrement du contrat.")
        except DjangoValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Contrat mis à jour.")
            return redirect('frontend:contrats-detail', pk=contrat.pk)

    return render(request, 'frontend/contrats/_modal_form.html', {
        **contexte_commun, 'erreurs': serializer.errors, 'valeurs': request.POST,
        'specificites_lignes': _lignes_specificites_depuis_post(request),
    })


@permission_requise('recouvrement.view_contrat')
def detail(request, pk):
    contrat = get_object_or_404(
        _contrats_accessibles(request.user).select_related(
            'chauffeur', 'type_contrat', 'agence', 'proprietaire', 'regle_generation',
        ),
        pk=pk,
    )
    sous_contrats = contrat.sous_contrats.select_related('type_contrat').order_by('id')
    leases = contrat.leases.order_by('-date_echeance')[:100]
    paiements = contrat.paiements.select_related('enregistre_par').order_by('-created_at')[:50]
    depenses = contrat.depenses.order_by('-date_depense')[:50]
    montant_total_depenses = contrat.depenses.aggregate(
        total=Sum('montant'),
    )['total'] or Decimal('0.00')

    # L'action annuler-leases/sous-contrats est routée sur ContratViewSet,
    # dont StrictDjangoModelPermissions calcule les perms depuis le modèle
    # Contrat quelle que soit l'action -> add_contrat pour ces deux POST.
    peut_gerer_contrat = request.user.has_perm('recouvrement.add_contrat')

    return render(request, 'frontend/contrats/detail.html', {
        'contrat': contrat,
        'sous_contrats': sous_contrats,
        'leases': leases,
        'paiements': paiements,
        'depenses': depenses,
        'montant_total_depenses': montant_total_depenses,
        'peut_modifier': request.user.has_perm('recouvrement.change_contrat'),
        'peut_ajouter_sous_contrat': peut_gerer_contrat,
        'peut_annuler_leases': peut_gerer_contrat,
        'peut_encaisser': request.user.has_perm('recouvrement.add_paiement'),
        'peut_ajouter_depense': request.user.has_perm('recouvrement.add_depense'),
        'peut_modifier_depense': request.user.has_perm('recouvrement.change_depense'),
    })


@permission_requise('recouvrement.add_contrat')
def ajouter_sous_contrat(request, pk):
    contrat_parent = get_object_or_404(_contrats_accessibles(request.user), pk=pk)

    if contrat_parent.parent_id is not None:
        messages.error(
            request,
            "Ce contrat est déjà un sous-contrat : impossible d'y rattacher un sous-contrat.",
        )
        return redirect('frontend:contrats-detail', pk=pk)

    options = {
        'types_contrat': TypeContrat.objects.filter(
            compte_id=request.user.compte_id, est_principal=False,
        ).order_by('libelle'),
    }
    contexte_commun = {
        'contrat_parent': contrat_parent,
        'options': options,
        'frequences': Contrat.FREQUENCE_CHOICES,
    }

    if request.method != 'POST':
        return render(request, 'frontend/contrats/_modal_sous_contrat.html', {
            **contexte_commun, 'erreurs': {},
            'specificites_lignes': _lignes_specificites(None),
        })

    donnees = request.POST.copy()
    donnees['specificites'] = _specificites_json_depuis_post(request)
    serializer = SousContratSerializer(
        data=donnees,
        context={'request': request, 'compte_id': contrat_parent.compte_id},
    )
    if serializer.is_valid():
        data = dict(serializer.validated_data)

        total = data.get('montant_total', Decimal('0.00'))
        avance = data.get('montant_paye', Decimal('0.00'))
        reste = max(Decimal('0.00'), total - avance)

        # Héritage forcé depuis le parent, à l'identique de
        # ContratViewSet.sous_contrats (proprietaire/agence/config_paiement
        # sont en lecture seule sur SousContratSerializer).
        data.update({
            'parent': contrat_parent,
            'agence': contrat_parent.agence,
            'proprietaire': contrat_parent.proprietaire,
            'chauffeur': contrat_parent.chauffeur,
            'compte_id': contrat_parent.compte_id,
            'nom_complet': contrat_parent.nom_complet,
            'enregistre_par': request.user,
            'config_paiement': contrat_parent.config_paiement,
            'montant_paye': avance,
            'montant_restant': reste,
            'statut': Contrat.STATUT_SOLDE if reste == 0 else Contrat.STATUT_ACTIF,
        })
        try:
            Contrat.objects.create(**data)
        except IntegrityError:
            messages.error(request, "Erreur d'enregistrement du sous-contrat.")
        except DjangoValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Sous-contrat ajouté.")
            return redirect('frontend:contrats-detail', pk=pk)

    return render(request, 'frontend/contrats/_modal_sous_contrat.html', {
        **contexte_commun, 'erreurs': serializer.errors,
        'specificites_lignes': _lignes_specificites_depuis_post(request),
    })


@permission_requise('recouvrement.add_contrat')
def annuler_leases(request, pk):
    if request.method != 'POST':
        return redirect('frontend:contrats-detail', pk=pk)

    contrat = get_object_or_404(_contrats_accessibles(request.user), pk=pk)
    lease_ids = request.POST.getlist('lease_ids')

    if not lease_ids:
        messages.error(request, "Sélectionnez au moins une échéance à annuler.")
        return redirect('frontend:contrats-detail', pk=pk)

    try:
        jours = max(0, int(request.POST.get('jours_a_prolonger') or 0))
    except ValueError:
        jours = 0

    # Portée déjà restreinte : le contrat vient de _contrats_accessibles(),
    # donc ses leases héritent de la même isolation tenant/permission.
    leases_a_annuler = contrat.leases.filter(id__in=lease_ids)

    try:
        resultat = annuler_leases_et_prolonger(leases_a_annuler, jours)
    except AnnulationLeaseError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"{resultat['nb_leases']} échéance(s) annulée(s)"
            + (f", {jours} jour(s) ouvré(s) ajoutés à la date de fin." if jours else "."),
        )
    return redirect('frontend:contrats-detail', pk=pk)
