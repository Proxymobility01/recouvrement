from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from core.filters import DepenseFilter
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.api.v1.serializers import DepenseSerializer
from recouvrement.models import Agence, Contrat, Depense


def _depenses_accessibles(user):
    return queryset_tenant(Depense, user).select_related(
        'contrat', 'contrat__chauffeur', 'agence', 'enregistre_par',
    )


def _contrats_pour_selection(user):
    return queryset_tenant(Contrat, user).select_related('chauffeur').order_by('-created_at')[:200]


@permission_requise('recouvrement.view_depense')
def liste(request):
    qs = _depenses_accessibles(request.user)

    # Catégorie, contrat, agence, plage de date et plage de montant : déjà
    # supportés par DepenseFilter (utilisé aussi par l'API).
    qs = DepenseFilter(request.GET, queryset=qs).qs

    page = Paginator(qs.order_by('-date_depense'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/depenses/list.html', {
        'page_obj': page,
        'filtres': request.GET,
        'categories': Depense.CATEGORIE_CHOICES,
        'contrats': _contrats_pour_selection(request.user),
        'agences': queryset_tenant(Agence, request.user).filter(actif=True).order_by('nom'),
        'querystring': querystring_sans_page(request),
        'peut_modifier': request.user.has_perm('recouvrement.change_depense'),
    })


@permission_requise('recouvrement.add_depense')
def creer(request):
    contrat_preselectionne = request.GET.get('contrat') or request.POST.get('contrat') or ''

    contexte_commun = {
        'depense': None,
        'contrats': _contrats_pour_selection(request.user),
        'categories': Depense.CATEGORIE_CHOICES,
    }

    if request.method != 'POST':
        return render(request, 'frontend/depenses/_modal_form.html', {
            **contexte_commun, 'erreurs': {}, 'valeurs': {'contrat': contrat_preselectionne},
        })

    serializer = DepenseSerializer(data=request.POST.copy(), context={'request': request})
    if serializer.is_valid():
        # compte_id n'est pas passé ici : DepenseSerializer.create() le
        # déduit toujours du contrat sélectionné (cf. validate_contrat).
        depense = serializer.save(enregistre_par=request.user)
        messages.success(request, f"Dépense « {depense.libelle} » enregistrée.")
        return redirect('frontend:contrats-detail', pk=depense.contrat_id)

    return render(request, 'frontend/depenses/_modal_form.html', {
        **contexte_commun, 'erreurs': serializer.errors, 'valeurs': request.POST,
    })


@permission_requise('recouvrement.change_depense')
def modifier(request, pk):
    depense = get_object_or_404(_depenses_accessibles(request.user), pk=pk)
    contexte_commun = {
        'depense': depense,
        'categories': Depense.CATEGORIE_CHOICES,
    }

    if request.method != 'POST':
        return render(request, 'frontend/depenses/_modal_form.html', {
            **contexte_commun, 'erreurs': {},
            'valeurs': {
                'categorie': depense.categorie,
                'libelle': depense.libelle,
                'description': depense.description,
                'montant': depense.montant,
                'date_depense': depense.date_depense.isoformat(),
                'fournisseur': depense.fournisseur,
            },
        })

    serializer = DepenseSerializer(
        depense, data=request.POST.copy(), context={'request': request}, partial=True,
    )
    if serializer.is_valid():
        serializer.save()
        messages.success(request, "Dépense mise à jour.")
        return redirect('frontend:contrats-detail', pk=depense.contrat_id)

    return render(request, 'frontend/depenses/_modal_form.html', {
        **contexte_commun, 'erreurs': serializer.errors, 'valeurs': request.POST,
    })
