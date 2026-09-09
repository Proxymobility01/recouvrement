from django.urls import path, include
from rest_framework.routers import DefaultRouter

from recouvrement.api.v1.views import (
    AgenceViewSet,
    ContratViewSet,
    LeaseViewSet,
    InitiationPaiementView,
    InitiationPaiementUSSDView,
    SoumissionPreuvePaiementUSSDView,
    WebhookView,
    PaiementViewSet,
    TypeContratViewSet,
    ParametreViewSet,
    ReglePenaliteViewSet,
    PenaliteViewSet, SessionPaiementViewSet, RegleGenerationLeaseViewSet,
    ProprietaireViewSet, CompteReceptionProprietaireViewSet,
    PreuvePaiementUSSDViewSet,
)


router = DefaultRouter()


router.register(r'agences', AgenceViewSet, basename='agence')
router.register(r'contrats', ContratViewSet, basename='contrat')
router.register(r'leases', LeaseViewSet, basename='lease')
router.register(r'paiements', PaiementViewSet, basename='paiement')
router.register(r'parametres', ParametreViewSet, basename='parametres')
router.register(r'type-contrats', TypeContratViewSet, basename='type-contrats')
router.register(r'regles-penalites', ReglePenaliteViewSet, basename='regle-penalite')
router.register(r'regles-gereration-leases', RegleGenerationLeaseViewSet, basename='regles-gereration-leases')
router.register(r'penalites', PenaliteViewSet, basename='penalite')
router.register(r'transactions', SessionPaiementViewSet, basename='session-paiement')
router.register(r'proprietaires', ProprietaireViewSet, basename='proprietaire')
router.register(
    r'comptes-reception-proprietaires',
    CompteReceptionProprietaireViewSet,
    basename='compte-reception-proprietaire',
)
router.register(
    r'preuves-paiement-ussd',
    PreuvePaiementUSSDViewSet,
    basename='preuve-paiement-ussd',
)
urlpatterns = [
    path('', include(router.urls)),
    path('initier-paiement/', InitiationPaiementView.as_view(), name='initier-paiement'),
    path(
        'initier-paiement-ussd/',
        InitiationPaiementUSSDView.as_view(),
        name='initier-paiement-ussd',
    ),
    path(
        'soumettre-preuve-paiement-ussd/',
        SoumissionPreuvePaiementUSSDView.as_view(),
        name='soumettre-preuve-paiement-ussd',
    ),
    path('webhook/', WebhookView.as_view(), name='webhook-paiement'),
]
