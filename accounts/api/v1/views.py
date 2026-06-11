import logging

from django.contrib.auth import get_user_model
from rest_framework.permissions import IsAuthenticated
from accounts.api.v1.serializers import GestionChauffeurSerializer
from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from core.permissions import StrictDjangoModelPermissions
from core.api.v1.views import TenantModelViewSet

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
        qs = super().get_queryset()
        # On ne liste que les utilisateurs qui sont "DRIVER"
        qs = qs.filter(roles__slug='DRIVER')
        return qs.order_by('-created_at')

    def perform_destroy(self, instance):
        """
        🚀 LOGIQUE INTELLIGENTE :
        - Hard Delete (Suppression) si le chauffeur n'a aucun historique métier.
        - Soft Delete (Désactivation) s'il a déjà des contrats ou des paiements liés.
        """
        user_connecte = self.request.user

        if instance.id == user_connecte.id:
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message="Action bloquée : Un utilisateur ne peut pas supprimer ou désactiver son propre compte."
            )

        # 1. Vérification de l'historique métier (A-t-il déjà touché au système ?)
        a_historique = (
            instance.contrats_a_payer.exists() or
            instance.paiements_effectues.exists() or
            instance.sessions_initiees.exists()

        )

        if a_historique:
            # 🔄 SOFT DELETE : Le chauffeur a des traces financières, on le désactive pour protéger la comptabilité.
            logger.warning(
                f"[AUDIT] DÉSACTIVATION Chauffeur id={instance.pk} compte_id={getattr(instance, 'compte_id', '?')} "
                f"par admin={user_connecte.email} (Raison: Historique métier existant)"
            )
            instance.is_active = False
            instance.save(update_fields=['is_active'])
        else:
            # 🗑️ HARD DELETE : Aucune trace métier, c'est probablement une erreur de saisie. On nettoie la BDD.
            logger.info(
                f"[AUDIT] SUPPRESSION AUTORISÉE pour le Chauffeur id={instance.pk} (Aucun historique métier trouvé)."
            )
            # On appelle le perform_destroy du parent (TenantModelViewSet) qui va se charger
            # de faire le instance.delete() et de générer le log d'audit de suppression.
            super().perform_destroy(instance)