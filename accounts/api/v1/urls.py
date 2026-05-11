from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import  GestionChauffeurViewSet

router = DefaultRouter()
router.register(r'chauffeurs', GestionChauffeurViewSet, basename='chauffeur')


urlpatterns = [
    path('', include(router.urls)),
]