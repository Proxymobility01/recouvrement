from django.urls import path, include
from rest_framework.routers import DefaultRouter

from recouvrement.api.v1.views import ContratViewSet, LeaseViewSet, InitiationPaiementView, MobilePaymentWebhookView

router = DefaultRouter()


router.register(r'contrats', ContratViewSet, basename='contrat')
router.register(r'leases', LeaseViewSet, basename='lease')

urlpatterns = [
    path('', include(router.urls)),
    path('initier-paiement/', InitiationPaiementView.as_view(), name='initier-paiement'),
    path('webhook/paiement/', MobilePaymentWebhookView.as_view(), name='webhook-paiement'),
]