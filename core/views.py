# Create your views here.
from rest_framework import viewsets
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes

class TenantModelViewSet(viewsets.ModelViewSet):
    """
    ViewSet de base pour l'isolation Multi-Tenant.
    ZÉRO rôle codé en dur. Sécurise le CRUD complet.
    """

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()

        if user.is_superuser:
            return qs

        return qs.filter(compte_id=user.compte_id)

    def perform_create(self, serializer):
        user = self.request.user

        if user.is_superuser:
            compte_id_fourni = self.request.data.get('compte_id')
            if not compte_id_fourni:
                raise CustomAPIException(
                    dev_msg="Missing 'compte_id' for superuser creation.",
                    resp_code=ErrorCodes.INVALID_PAYLOAD,
                    status_code=400,
                    usr_msg="En tant que Super Administrateur, précisez le compte_id cible."
                )
            serializer.save(compte_id=compte_id_fourni)
        else:
            serializer.save(compte_id=user.compte_id)

    # NOUVEAU : On blinde le "Update"
    def perform_update(self, serializer):
        user = self.request.user

        if user.is_superuser:
            serializer.save()
        else:
            # On écrase silencieusement toute tentative de modification du compte_id
            # en forçant l'ID de l'utilisateur connecté.
            serializer.save(compte_id=user.compte_id)