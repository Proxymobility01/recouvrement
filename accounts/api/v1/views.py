from django.contrib.auth import get_user_model
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions
from accounts.api.v1.serializers import UtilisateurListDetailSerializer
from core.views import TenantModelViewSet


User = get_user_model()


class UtilisateurViewSet(TenantModelViewSet):
    """
    API en lecture seule.
    Utilise exclusivement les permissions Django pour le filtrage des données.
    """
    queryset = User.objects.all()
    serializer_class = UtilisateurListDetailSerializer

    # 1. Le Gardien : Vérifie si l'utilisateur a le droit d'utiliser l'endpoint
    permission_classes = [IsAuthenticated, DjangoModelPermissions]

    http_method_names = ['get', 'head', 'options']

    def get_queryset(self):
        # 2. Isolation Multi-Tenant (Gérée par ton TenantModelViewSet)
        queryset = super().get_queryset()
        user = self.request.user

        # 3. Isolation de Visibilité (Data Scoping) via Permissions
        # On vérifie si l'utilisateur possède la permission globale de voir tous les comptes
        # Format : 'nom_de_lapp.nom_de_la_permission'
        if user.is_superuser or user.has_perm('accounts.view_all_users'):
            return queryset.order_by('-created_at')

        # 4. Si l'utilisateur n'a pas la permission de tout voir, il ne voit que lui-même
        return queryset.filter(id=user.id).order_by('-created_at')