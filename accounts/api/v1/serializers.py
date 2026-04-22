from rest_framework import serializers
from django.contrib.auth import get_user_model

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
