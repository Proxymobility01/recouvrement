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

        # Le SuperAdmin voit toutes les données de toutes les entreprises
        if user.is_superuser:
            return qs

        # Les utilisateurs normaux sont cloisonnés dans leur entreprise
        return qs.filter(compte_id=user.compte_id)

    def perform_create(self, serializer):
        user = self.request.user

        if user.is_superuser:
            # Un superadmin crée pour le compte de quelqu'un d'autre
            compte_id_fourni = self.request.data.get('compte_id')

            if not compte_id_fourni:
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message="En tant que super-administrateur, vous devez explicitement fournir le 'compte_id' cible."
                )

            try:
                # 🚀 ROBUSTESSE : Vérification stricte du type
                compte_id_fourni = int(compte_id_fourni)
            except (TypeError, ValueError):
                raise CustomAPIException(
                    resp_code=ErrorCodes.BAD_REQUEST,
                    status_code=400,
                    dev_message=f"Le 'compte_id' fourni n'est pas un nombre entier valide. Valeur reçue : {compte_id_fourni}"
                )

            serializer.save(compte_id=compte_id_fourni)
        else:
            # Création standard pour un agent classique (Cloisonnement forcé)
            serializer.save(compte_id=user.compte_id)

    def perform_update(self, serializer):
        user = self.request.user

        if user.is_superuser:
            serializer.save()
        else:
            # Écrase silencieusement toute tentative de modification du compte_id par un pirate/utilisateur
            serializer.save(compte_id=user.compte_id)

    def perform_destroy(self, instance):
        # 🚀 TRAÇABILITÉ : Log d'audit obligatoire pour toute suppression système
        logger.warning(
            f"[AUDIT] DELETE {instance.__class__.__name__} "
            f"id={instance.pk} compte_id={getattr(instance, 'compte_id', '?')} "
            f"par user={self.request.user.keycloak_id}"
        )

        # Note : Pas besoin de try/except DatabaseError ici, DRF s'en charge très bien
        # et notre custom_exception_handler renverra un beau 500 si la requête SQL échoue.
        instance.delete()