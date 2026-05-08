from django.urls import path, include
from rest_framework.routers import DefaultRouter

from statistiques.api.v1.views import StatistiqueJournaliereViewSet

router = DefaultRouter()

router.register(r'statistiques', StatistiqueJournaliereViewSet, basename='statistiques')

urlpatterns = [
    path('', include(router.urls)),
]