# Create your views here.
import logging

from rest_framework import viewsets
from core.exceptions import CustomAPIException
from core.errors import ErrorCodes
logger = logging.getLogger(__name__)
class TenantModelViewSet(viewsets.ModelViewSet):
    """
    ViewSet de base pour l'isolation Multi-Tenant.
    Toute ressource est automatiquement filtrée par compte_id.
    """

    def get_queryset(self):
        # 🚀 SÉCURITÉ : Force le développeur à définir un queryset
        assert self.queryset is not None, (
            f"'{self.__class__.__name__}' doit définir un attribut 'queryset'."
        )
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
            try:
                # 🚀 ROBUSTESSE : Vérification du type
                compte_id_fourni = int(compte_id_fourni)
            except (TypeError, ValueError):
                raise CustomAPIException(
                    dev_msg=f"Invalid compte_id: {compte_id_fourni}",
                    resp_code=ErrorCodes.INVALID_PAYLOAD,
                    status_code=400,
                    usr_msg="Le compte_id doit être un entier valide."
                )
            serializer.save(compte_id=compte_id_fourni)
        else:
            serializer.save(compte_id=user.compte_id)

    def perform_update(self, serializer):
        user = self.request.user
        if user.is_superuser:
            serializer.save()
        else:
            # Écrase silencieusement toute tentative de modification du compte_id
            serializer.save(compte_id=user.compte_id)

    def perform_destroy(self, instance):
        # 🚀 TRAÇABILITÉ : Log d'audit obligatoire pour toute suppression système
        logger.warning(
            f"[AUDIT] DELETE {instance.__class__.__name__} "
            f"id={instance.pk} compte_id={getattr(instance, 'compte_id', '?')} "
            f"par user={self.request.user.keycloak_id}" # ou self.request.user.email
        )
        instance.delete()