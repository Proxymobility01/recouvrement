from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import UtilisateurViewSet, GestionChauffeurViewSet

router = DefaultRouter()
# router.register(r'users', UtilisateurViewSet, basename='users')
router.register(r'chauffeurs', GestionChauffeurViewSet, basename='chauffeur')


urlpatterns = [
    path('', include(router.urls)),
]