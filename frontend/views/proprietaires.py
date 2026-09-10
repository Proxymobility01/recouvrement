from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.api.v1.serializers import CompteReceptionProprietaireSerializer, ProprietaireSerializer
from recouvrement.models import Proprietaire


@permission_requise('recouvrement.view_proprietaire')
def liste(request):
    qs = queryset_tenant(Proprietaire, request.user).prefetch_related('comptes_reception')

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(
            Q(nom_complet__icontains=recherche) | Q(comptes_reception__numero__icontains=recherche)
        ).distinct()

    actif = request.GET.get('actif', '')
    if actif == '1':
        qs = qs.filter(actif=True)
    elif actif == '0':
        qs = qs.filter(actif=False)

    page = Paginator(qs.order_by('nom_complet'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/proprietaires/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'actif': actif,
        'querystring': querystring_sans_page(request),
    })


@permission_requise('recouvrement.add_proprietaire')
def creer(request):
    if request.method == 'POST':
        serializer = ProprietaireSerializer(data=request.POST.copy(), context={'request': request})
        if serializer.is_valid():
            proprietaire = serializer.save(compte_id=request.user.compte_id)
            messages.success(request, f"Propriétaire {proprietaire.nom_complet} créé.")
            return redirect('frontend:proprietaires-detail', pk=proprietaire.pk)
        return render(request, 'frontend/proprietaires/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'proprietaire': None,
        })

    return render(request, 'frontend/proprietaires/_modal_form.html', {
        'erreurs': {}, 'valeurs': {}, 'proprietaire': None,
    })


@permission_requise('recouvrement.change_proprietaire')
def modifier(request, pk):
    proprietaire = get_object_or_404(queryset_tenant(Proprietaire, request.user), pk=pk)

    if request.method == 'POST':
        serializer = ProprietaireSerializer(
            proprietaire, data=request.POST.copy(), context={'request': request}, partial=True,
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, "Propriétaire mis à jour.")
            return redirect('frontend:proprietaires-detail', pk=proprietaire.pk)
        return render(request, 'frontend/proprietaires/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'proprietaire': proprietaire,
        })

    return render(request, 'frontend/proprietaires/_modal_form.html', {
        'erreurs': {},
        'valeurs': {'nom_complet': proprietaire.nom_complet, 'actif': proprietaire.actif},
        'proprietaire': proprietaire,
    })


@permission_requise('recouvrement.view_proprietaire')
def detail(request, pk):
    proprietaire = get_object_or_404(
        queryset_tenant(Proprietaire, request.user).prefetch_related('comptes_reception'), pk=pk,
    )
    return render(request, 'frontend/proprietaires/detail.html', {
        'proprietaire': proprietaire,
        'comptes_reception': proprietaire.comptes_reception.all().order_by('operateur'),
        'peut_modifier': request.user.has_perm('recouvrement.change_proprietaire'),
        'peut_ajouter_compte': request.user.has_perm('recouvrement.add_comptereceptionproprietaire'),
        'peut_gerer_comptes': request.user.has_perm('recouvrement.change_comptereceptionproprietaire'),
    })


@permission_requise('recouvrement.add_comptereceptionproprietaire')
def ajouter_compte_reception(request, pk):
    proprietaire = get_object_or_404(queryset_tenant(Proprietaire, request.user), pk=pk)

    if request.method == 'POST':
        donnees = request.POST.copy()
        donnees['proprietaire'] = proprietaire.pk
        serializer = CompteReceptionProprietaireSerializer(
            data=donnees, context={'request': request},
        )
        if serializer.is_valid():
            try:
                serializer.save(compte_id=request.user.compte_id)
            except IntegrityError:
                messages.error(
                    request,
                    "Ce propriétaire a déjà un compte pour cet opérateur, ou ce "
                    "numéro est déjà utilisé pour cet opérateur dans votre entreprise.",
                )
            else:
                messages.success(request, "Compte de réception ajouté.")
                return redirect('frontend:proprietaires-detail', pk=proprietaire.pk)

        return render(request, 'frontend/proprietaires/_modal_compte_reception.html', {
            'proprietaire': proprietaire, 'erreurs': serializer.errors,
        })

    return render(request, 'frontend/proprietaires/_modal_compte_reception.html', {
        'proprietaire': proprietaire, 'erreurs': {},
    })


@permission_requise('recouvrement.change_comptereceptionproprietaire')
def basculer_compte_reception(request, pk, compte_pk):
    """Active/désactive un compte de réception (bascule simple, sans validation métier)."""
    if request.method != 'POST':
        return redirect('frontend:proprietaires-detail', pk=pk)

    proprietaire = get_object_or_404(queryset_tenant(Proprietaire, request.user), pk=pk)
    compte = get_object_or_404(proprietaire.comptes_reception, pk=compte_pk)
    compte.actif = not compte.actif
    compte.save(update_fields=['actif', 'updated_at'])

    messages.success(
        request,
        f"Compte {compte.numero} ({compte.get_operateur_display()}) "
        + ("activé." if compte.actif else "désactivé."),
    )
    return redirect('frontend:proprietaires-detail', pk=pk)
