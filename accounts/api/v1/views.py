import logging

from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions
from accounts.api.v1.serializers import GestionChauffeurSerializer
from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from core.permissions import StrictDjangoModelPermissions
from core.views import TenantModelViewSet

User = get_user_model()

logger = logging.getLogger(__name__)
class GestionChauffeurViewSet(TenantModelViewSet):
    """
    API complète (CRUD) pour gérer les chauffeurs d'un compte partenaire.
    Hérite de TenantModelViewSet pour l'isolation stricte des données.
    """
    queryset = User.objects.all()
    serializer_class = GestionChauffeurSerializer
    permission_classes = [IsAuthenticated, StrictDjangoModelPermissions]
    def get_queryset(self):
        # 1. Le TenantModelViewSet isole déjà par 'compte_id'
        qs = super().get_queryset()

        # 2. 🎯 OPTIONNEL MAIS RECOMMANDÉ : On ne liste que les utilisateurs qui sont "DRIVER"
        # Pour éviter que l'Admin ne voie d'autres Admins dans sa liste de chauffeurs à modifier
        qs = qs.filter(roles__slug='DRIVER')

        return qs.order_by('-created_at')

    # 💡 perform_create et perform_update sont DÉJÀ gérés à la perfection
    # par ton TenantModelViewSet (qui injecte et protège le compte_id).
    # Pas besoin de les réécrire !

    def perform_destroy(self, instance):
        """
        🚀 SOFT DELETE : Règle d'or en comptabilité.
        On ne supprime jamais un chauffeur. On le désactive.
        """
        user_connecte = self.request.user

        if instance.id == user_connecte.id:
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message="Soft-delete bloqué : Un utilisateur ne peut pas désactiver son propre compte."
            )

        logger.warning(
            f"[AUDIT] DÉSACTIVATION Chauffeur id={instance.pk} compte_id={getattr(instance, 'compte_id', '?')} "
            f"par admin={user_connecte.email}"
        )

        # Désactivation au lieu de suppression SQL
        instance.is_active = False
        instance.save(update_fields=['is_active'])