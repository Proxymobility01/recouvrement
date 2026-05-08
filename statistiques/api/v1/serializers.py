from rest_framework import serializers
from statistiques.models import StatistiqueJournaliere # Ajuste l'import selon ton app

class StatistiqueJournaliereSerializer(serializers.ModelSerializer):
    class Meta:
        model = StatistiqueJournaliere
        fields = [
            'id',
            'date',
            'montant_attendu',
            'montant_collecte',
            'montant_echec',
            'total_attendus',
            'ayant_verse',
            'n_ayant_pas_verse',
            'updated_at'
        ]

        read_only_fields = fields