from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from core.exceptions import CustomAPIException
from core.filters import SessionPaiementFilter
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.models import Agence, SessionPaiement
from recouvrement.services_ussd import PaiementUSSDService


def _transactions_accessibles(user):
    qs = queryset_tenant(SessionPaiement, user).select_related(
        'utilisateur', 'agence', 'proprietaire', 'compte_reception',
        'config_paiement', 'preuve_ussd',
    )
    if user.is_superuser or user.has_perm('recouvrement.view_all_sessionpaiements'):
        return qs
    return qs.filter(utilisateur=user)


@permission_requise('recouvrement.view_sessionpaiement')
def liste(request):
    """Lecture seule, comme SessionPaiementViewSet : aucune écriture ici."""
    qs = _transactions_accessibles(request.user)

    # Statut, canal, agence, plage de date de validation et plage de
    # montant : déjà supportés par SessionPaiementFilter (utilisé aussi
    # par l'API).
    qs = SessionPaiementFilter(request.GET, queryset=qs).qs

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(Q(reference__icontains=recherche) | Q(telephone__icontains=recherche))

    page = Paginator(qs.order_by('-created_at'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/transactions/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'filtres': request.GET,
        'statuts': SessionPaiement.STATUT_CHOICES,
        'canaux': SessionPaiement.CANAL_CHOICES,
        'agences': queryset_tenant(Agence, request.user).filter(actif=True).order_by('nom'),
        'querystring': querystring_sans_page(request),
    })


@permission_requise('recouvrement.view_sessionpaiement')
def detail(request, pk):
    session = get_object_or_404(_transactions_accessibles(request.user), pk=pk)
    lignes = session.lignes_paiement.select_related('contrat', 'lease').order_by('id')
    preuve = getattr(session, 'preuve_ussd', None)

    return render(request, 'frontend/transactions/detail.html', {
        'session': session,
        'lignes': lignes,
        'preuve': preuve,
        # Même exigence double que PreuvePaiementUSSDViewSet.valider/rejeter :
        # voir la preuve ET avoir la permission métier dédiée.
        'peut_verifier_ussd': request.user.has_perms([
            'recouvrement.view_preuvepaiementussd',
            'recouvrement.can_validate_ussd_payment',
        ]),
    })


@permission_requise('recouvrement.view_preuvepaiementussd', 'recouvrement.can_validate_ussd_payment')
def valider_preuve_ussd(request, pk):
    if request.method != 'POST':
        return redirect('frontend:transactions-detail', pk=pk)

    session = get_object_or_404(_transactions_accessibles(request.user), pk=pk)
    preuve = getattr(session, 'preuve_ussd', None)
    if preuve is None:
        messages.error(request, "Aucune preuve USSD associée à cette session.")
        return redirect('frontend:transactions-detail', pk=pk)

    try:
        _, modifiee = PaiementUSSDService.valider_preuve(preuve_id=preuve.id, agent=request.user)
    except CustomAPIException as exc:
        messages.error(request, exc.dev_message)
    else:
        messages.success(
            request,
            "Preuve validée. La ventilation des paiements a été déclenchée."
            if modifiee else "Cette preuve était déjà validée.",
        )
    return redirect('frontend:transactions-detail', pk=pk)


@permission_requise('recouvrement.view_preuvepaiementussd', 'recouvrement.can_validate_ussd_payment')
def rejeter_preuve_ussd(request, pk):
    if request.method != 'POST':
        return redirect('frontend:transactions-detail', pk=pk)

    session = get_object_or_404(_transactions_accessibles(request.user), pk=pk)
    preuve = getattr(session, 'preuve_ussd', None)
    motif = request.POST.get('motif', '').strip()

    if preuve is None:
        messages.error(request, "Aucune preuve USSD associée à cette session.")
        return redirect('frontend:transactions-detail', pk=pk)
    if not motif:
        messages.error(request, "Le motif du rejet est obligatoire.")
        return redirect('frontend:transactions-detail', pk=pk)

    try:
        _, modifiee = PaiementUSSDService.rejeter_preuve(
            preuve_id=preuve.id, agent=request.user, motif=motif,
        )
    except CustomAPIException as exc:
        messages.error(request, exc.dev_message)
    else:
        messages.success(
            request,
            "Preuve rejetée. Les échéances concernées peuvent être payées à nouveau."
            if modifiee else "Cette preuve était déjà rejetée.",
        )
    return redirect('frontend:transactions-detail', pk=pk)
