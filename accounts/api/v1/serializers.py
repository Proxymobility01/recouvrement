from rest_framework import serializers
from django.contrib.auth import get_user_model

from accounts.models import Role, CustomUserRole

User = get_user_model()

class UtilisateurListDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'nom_complet',
            'is_active',
        ]


class GestionChauffeurSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id',
            'keycloak_id',
            'email',
            'nom_complet',
            # Si tu as d'autres champs comme téléphone, tu peux les rajouter
            'is_active'
        ]

    def create(self, validated_data):
        # 1. Création du compte utilisateur local
        user = super().create(validated_data)

        # 2. Sécurité : Désactivation du login local
        user.set_unusable_password()
        user.save()

        # 3. 🚀 CRÉATION EXPLICITE DU LIEN UTILISATEUR <-> RÔLE
        try:
            role_driver = Role.objects.get(slug='DRIVER')

            # On utilise ton modèle CustomUserRole pour respecter tes contraintes
            # et le clean() que tu as écrit !
            CustomUserRole.objects.create(
                user=user,
                role=role_driver,
                compte_id=user.compte_id,
                principal=True,  # 💡 Puisque c'est son seul rôle à la création, on le met en principal
                actif=True  # Par défaut c'est True dans ton modèle, mais c'est bien de l'expliciter

                # Note: assigne_par est optionnel dans ton modèle (null=True),
                # donc on peut le laisser vide, ou le remplir avec self.context['request'].user si besoin.
            )

        except Role.DoesNotExist:
            # Sécurité au cas où le rôle n'a pas encore été importé
            pass

        return user
