import functools

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def permission_requise(*perms):
    """
    Équivalent de StrictDjangoModelPermissions pour les vues classiques :
    exige l'authentification puis les permissions Django données
    (ex: 'recouvrement.view_contrat'), avec les mêmes codenames que l'API.
    """
    def decorateur(vue):
        @login_required
        @functools.wraps(vue)
        def wrapper(request, *args, **kwargs):
            if not request.user.has_perms(perms):
                raise PermissionDenied(
                    "Vous n'avez pas la permission d'effectuer cette action."
                )
            return vue(request, *args, **kwargs)
        return wrapper
    return decorateur


def queryset_tenant(model, user):
    """Reproduit TenantModelViewSet.get_queryset : isolation stricte par compte_id."""
    qs = model.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(compte_id=user.compte_id)


def querystring_sans_page(request):
    """Querystring GET actuelle sans `page`, pour construire les liens de pagination."""
    qs = request.GET.copy()
    qs.pop('page', None)
    return qs.urlencode()
