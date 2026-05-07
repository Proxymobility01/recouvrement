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
                # 🚀 Utilisation de la nouvelle architecture
                raise CustomAPIException(
                    resp_code=ErrorCodes.TENANT_SUPERUSER_MISSING_ID,
                    status_code=400
                )

            try:
                # 🚀 ROBUSTESSE : Vérification du type
                compte_id_fourni = int(compte_id_fourni)
            except (TypeError, ValueError):
                # 🚀 Utilisation de la nouvelle architecture avec contexte
                raise CustomAPIException(
                    resp_code=ErrorCodes.TENANT_INVALID_ID_FORMAT,
                    status_code=400,
                    context=f"Valeur reçue: {compte_id_fourni}"
                )

            serializer.save(compte_id=compte_id_fourni)
        else:
            # Création standard pour un agent classique
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
            f"par user={self.request.user.keycloak_id}"  # ou self.request.user.email
        )
        instance.delete()