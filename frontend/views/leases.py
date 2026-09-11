from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from core.filters import LeaseFilter
from frontend.mixins import permission_requise, queryset_tenant, querystring_sans_page
from recouvrement.models import Agence, Lease


def _leases_accessibles(user):
    qs = queryset_tenant(Lease, user).select_related(
        'contrat', 'contrat__chauffeur', 'contrat__type_contrat', 'agence',
    )
    if user.is_superuser or user.has_perm('recouvrement.view_all_leases'):
        return qs
    return qs.filter(contrat__chauffeur=user)


@permission_requise('recouvrement.view_lease')
def liste(request):
    """Lecture seule, comme LeaseViewSet : aucune création/modification de lease ici."""
    qs = _leases_accessibles(request.user)

    # Statut, agence et plage de date d'échéance : déjà supportés par
    # LeaseFilter (utilisé aussi par l'API), réutilisé ici tel quel.
    qs = LeaseFilter(request.GET, queryset=qs).qs

    recherche = request.GET.get('q', '').strip()
    if recherche:
        qs = qs.filter(
            Q(nom_complet__icontains=recherche) | Q(contrat__reference__icontains=recherche)
        )

    page = Paginator(qs.order_by('-date_echeance'), 25).get_page(request.GET.get('page'))

    return render(request, 'frontend/leases/list.html', {
        'page_obj': page,
        'recherche': recherche,
        'filtres': request.GET,
        'statuts': Lease.STATUT_CHOICES,
        'agences': queryset_tenant(Agence, request.user).filter(actif=True).order_by('nom'),
        'querystring': querystring_sans_page(request),
    })
