from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from accounts.api.v1.serializers import GestionChauffeurSerializer
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.models import Contrat

CustomUser = get_user_model()


def _chauffeurs_accessibles(user):
    return queryset_tenant(CustomUser, user).filter(roles__slug='DRIVER').distinct()


def _valeurs_depuis_chauffeur(chauffeur):
    if chauffeur is None:
        return {}
    return {
        'keycloak_id': chauffeur.keycloak_id,
        'email': chauffeur.email or '',
        'nom_complet': chauffeur.nom_complet,
        'is_active': chauffeur.is_active,
    }


@permission_requise('accounts.view_customuser')
def liste(request):
    qs = _chauffeurs_accessibles(request.user)

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(Q(nom_complet__icontains=recherche) | Q(email__icontains=recherche))

    actif = request.GET.get('actif', '')
    if actif == '1':
        qs = qs.filter(is_active=True)
    elif actif == '0':
        qs = qs.filter(is_active=False)

    page = Paginator(qs.order_by('-created_at'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/chauffeurs/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'actif': actif,
        'querystring': querystring_sans_page(request),
    })


@permission_requise('accounts.add_customuser')
def creer(request):
    if request.method == 'POST':
        serializer = GestionChauffeurSerializer(
            data=request.POST.copy(), context={'request': request},
        )
        if serializer.is_valid():
            chauffeur = serializer.save(compte_id=request.user.compte_id)
            messages.success(request, f"Chauffeur {chauffeur.nom_complet} créé.")
            return redirect('frontend:chauffeurs-detail', pk=chauffeur.pk)
        return render(request, 'frontend/chauffeurs/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'chauffeur': None,
        })

    return render(request, 'frontend/chauffeurs/_modal_form.html', {
        'erreurs': {}, 'valeurs': {}, 'chauffeur': None,
    })


@permission_requise('accounts.change_customuser')
def modifier(request, pk):
    chauffeur = get_object_or_404(_chauffeurs_accessibles(request.user), pk=pk)

    if request.method == 'POST':
        serializer = GestionChauffeurSerializer(
            chauffeur, data=request.POST.copy(), context={'request': request}, partial=True,
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, "Chauffeur mis à jour.")
            return redirect('frontend:chauffeurs-detail', pk=chauffeur.pk)
        return render(request, 'frontend/chauffeurs/_modal_form.html', {
            'erreurs': serializer.errors, 'valeurs': request.POST, 'chauffeur': chauffeur,
        })

    return render(request, 'frontend/chauffeurs/_modal_form.html', {
        'erreurs': {}, 'valeurs': _valeurs_depuis_chauffeur(chauffeur), 'chauffeur': chauffeur,
    })


@permission_requise('accounts.view_customuser')
def detail(request, pk):
    chauffeur = get_object_or_404(_chauffeurs_accessibles(request.user), pk=pk)
    contrats = queryset_tenant(Contrat, request.user).filter(
        chauffeur=chauffeur,
    ).select_related('type_contrat').order_by('-created_at')[:20]

    return render(request, 'frontend/chauffeurs/detail.html', {
        'chauffeur': chauffeur,
        'contrats': contrats,
        'peut_modifier': request.user.has_perm('accounts.change_customuser'),
        'peut_supprimer': request.user.has_perm('accounts.delete_customuser'),
    })


@permission_requise('accounts.delete_customuser')
def supprimer(request, pk):
    if request.method != 'POST':
        return redirect('frontend:chauffeurs-detail', pk=pk)

    chauffeur = get_object_or_404(_chauffeurs_accessibles(request.user), pk=pk)

    if chauffeur.id == request.user.id:
        messages.error(request, "Vous ne pouvez pas supprimer ou désactiver votre propre compte.")
        return redirect('frontend:chauffeurs-detail', pk=pk)

    # Même logique intelligente que GestionChauffeurViewSet.perform_destroy :
    # suppression réelle seulement sans historique métier, sinon désactivation.
    a_historique = (
        chauffeur.contrats_a_payer.exists()
        or chauffeur.paiements_effectues.exists()
        or chauffeur.sessions_initiees.exists()
    )
    if a_historique:
        chauffeur.is_active = False
        chauffeur.save(update_fields=['is_active'])
        messages.success(
            request,
            f"{chauffeur.nom_complet} a un historique métier : compte désactivé (pas supprimé).",
        )
        return redirect('frontend:chauffeurs-detail', pk=pk)

    nom = chauffeur.nom_complet
    chauffeur.delete()
    messages.success(request, f"{nom} supprimé (aucun historique métier).")
    return redirect('frontend:chauffeurs-liste')
