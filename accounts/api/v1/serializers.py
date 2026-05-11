from rest_framework import serializers
from django.contrib.auth import get_user_model
from accounts.models import Role, CustomUserRole

User = get_user_model()


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


        try:
            role_driver = Role.objects.get(slug='DRIVER')


            CustomUserRole.objects.create(
                user=user,
                role=role_driver,
                compte_id=user.compte_id,
                principal=True,
                actif=True
            )

        except Role.DoesNotExist:
            pass

        return user
