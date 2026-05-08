from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, DjangoModelPermissions
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from core.views import TenantModelViewSet
from statistiques.api.v1.serializers import StatistiqueJournaliereSerializer
from statistiques.models import StatistiqueJournaliere


# Assure-toi d'importer ton TenantModelViewSet
# from core.views import TenantModelViewSet

class StatistiqueJournaliereViewSet(TenantModelViewSet):
    """
    ViewSet pour la consultation des statistiques financières et de recouvrement.
    """
    queryset = StatistiqueJournaliere.objects.all()
    serializer_class = StatistiqueJournaliereSerializer

    # Ajuste tes permissions selon ce que tu utilises d'habitude
    permission_classes = [IsAuthenticated,DjangoModelPermissions]

    # Activation des filtres
    filter_backends = [DjangoFilterBackend]

    # 🚀 Permet au front de faire une requête : GET /api/v1/statistiques/?date=2026-05-01
    filterset_fields = ['date']

    # Par défaut, si on liste tout, on met la date la plus récente en premier
    ordering = ['-date']

    # --- HTTP METHODS AUTORISÉES ---
    # On bloque explicitement POST, PUT, PATCH et DELETE pour la sécurité.
    http_method_names = ['get', 'head', 'options']

    @action(detail=False, methods=['get'], url_path='du-jour')
    def du_jour(self, request):
        """
        Raccourci spécifiquement conçu pour le Dashboard principal.
        Endpoint: GET /api/v1/statistiques/du-jour/
        Avantage : Renvoie une structure propre avec des 0 si aucune stat n'existe encore.
        """
        aujourdhui = timezone.now().date()

        # Le get_queryset() applique déjà le filtre de ton TenantModelViewSet (compte_id)
        stats = self.get_queryset().filter(date=aujourdhui).first()

        if not stats:
            # Structure par défaut si le script n'a pas encore tourné
            return Response({
                "date": aujourdhui,
                "message": "Calcul en attente.",
                "montant_attendu": 0,
                "montant_collecte": 0,
                "montant_echec": 0,
                "total_attendus": 0,
                "ayant_verse": 0,
                "n_ayant_pas_verse": 0,
                "updated_at": None
            })

        # Si les stats existent, on passe l'objet au Serializer
        serializer = self.get_serializer(stats)
        return Response(serializer.data)