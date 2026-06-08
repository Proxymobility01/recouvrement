import logging

from rest_framework import serializers
from django.contrib.auth import get_user_model
from accounts.models import Role, CustomUserRole

User = get_user_model()
logger = logging.getLogger(__name__)

class GestionChauffeurSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id',
            'keycloak_id',
            'email',
            'nom_complet',
            'is_active'
        ]

    def create(self, validated_data):
        # 1. Création du compte utilisateur local
        user = super().create(validated_data)

        # 2. Sécurité : Désactivation du login local
        user.set_unusable_password()
        user.save()
        admin_createur = self.context['request'].user


        try:
            role_driver = Role.objects.get(slug='DRIVER')
            compte_id_int = int(user.compte_id)

            CustomUserRole.objects.create(
                user=user,
                role=role_driver,
                compte_id=compte_id_int,
                principal=True,
                actif=True,
                assigne_par=admin_createur
            )

        except Role.DoesNotExist:
            logger.exception(
                f"[ALERTE] Le rôle 'DRIVER' n'existe pas en BDD. "
                f"Le chauffeur {user.email} (ID: {user.id}) a été créé sans rôle !"
            )

        return user
