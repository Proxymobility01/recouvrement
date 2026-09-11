from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import F, Q
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.shortcuts import get_object_or_404, redirect, render

from core.filters import PaiementFilter
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.api.v1.serializers import PaiementSerializer
from recouvrement.models import Agence, Lease, Paiement


def _paiements_accessibles(user):
    qs = queryset_tenant(Paiement, user).select_related(
        'contrat', 'contrat__chauffeur', 'enregistre_par', 'lease',
    )
    if user.is_superuser or user.has_perm('recouvrement.view_all_paiements'):
        return qs
    return qs.filter(Q(contrat__chauffeur=user) | Q(enregistre_par=user))


def _leases_payables(user):
    qs = queryset_tenant(Lease, user).exclude(
        statut__in=[Lease.STATUT_PAYE, Lease.STATUT_ANNULE],
    ).select_related('contrat', 'contrat__chauffeur').annotate(
        reste=F('montant_attendu') - F('montant_paye'),
    )
    if user.is_superuser or user.has_perm('recouvrement.view_all_leases'):
        return qs
    return qs.filter(Q(contrat__chauffeur=user) | Q(contrat__enregistre_par=user))


@permission_requise('recouvrement.view_paiement')
def liste(request):
    qs = _paiements_accessibles(request.user)

    # Statut, méthode, agence, plage de date de paiement et plage de montant :
    # déjà supportés par PaiementFilter (utilisé aussi par l'API).
    qs = PaiementFilter(request.GET, queryset=qs).qs

    page = Paginator(qs.order_by('-created_at'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/paiements/list.html', {
        'page_obj': page,
        'filtres': request.GET,
        'statuts': Paiement.STATUT_CHOICES,
        'methodes': Paiement.METHODE_CHOICES,
        'agences': queryset_tenant(Agence, request.user).filter(actif=True).order_by('nom'),
        'querystring': querystring_sans_page(request),
    })


@permission_requise('recouvrement.add_paiement')
def creer(request):
    lease_preselectionnee = request.GET.get('lease') or request.POST.get('lease_id') or ''

    if request.method == 'POST':
        serializer = PaiementSerializer(
            data=request.POST.copy(), context={'request': request},
        )
        if serializer.is_valid():
            try:
                paiement = serializer.save(enregistre_par=request.user)
            except IntegrityError:
                messages.error(request, "Erreur d'enregistrement du paiement.")
            else:
                messages.success(request, f"Paiement de {paiement.montant} F enregistré.")
                return redirect('frontend:contrats-detail', pk=paiement.contrat_id)

        return render(request, 'frontend/paiements/_modal_form.html', {
            'erreurs': serializer.errors,
            'leases': _leases_payables(request.user).order_by('date_echeance')[:200],
            'lease_preselectionnee': lease_preselectionnee,
            'paiement': None,
        })

    return render(request, 'frontend/paiements/_modal_form.html', {
        'erreurs': {},
        'leases': _leases_payables(request.user).order_by('date_echeance')[:200],
        'lease_preselectionnee': lease_preselectionnee,
        'paiement': None,
    })


@permission_requise('recouvrement.change_paiement')
def modifier(request, pk):
    paiement = get_object_or_404(_paiements_accessibles(request.user), pk=pk)

    if request.method != 'POST':
        return render(request, 'frontend/paiements/_modal_form.html', {
            'erreurs': {}, 'paiement': paiement,
        })

    serializer = PaiementSerializer(
        paiement, data=request.POST.copy(), context={'request': request}, partial=True,
    )
    try:
        if serializer.is_valid():
            serializer.save()
            messages.success(request, "Paiement mis à jour.")
            return redirect('frontend:contrats-detail', pk=paiement.contrat_id)
    except DRFValidationError as exc:
        # PaiementSerializer.update() lève cette erreur directement dans
        # .save() (pas pendant is_valid()) pour bloquer l'édition d'un
        # paiement Mobile Money.
        messages.error(request, str(exc.detail))

    return render(request, 'frontend/paiements/_modal_form.html', {
        'erreurs': serializer.errors, 'paiement': paiement,
    })
