from django.urls import path

from frontend.views import (
    auth, chauffeurs, contrats, leases, paiements, proprietaires, transactions, types_contrat,
)

app_name = 'frontend'

urlpatterns = [
    path('login/', auth.login_view, name='login'),
    path('login/callback/', auth.login_callback_view, name='login-callback'),
    path('logout/', auth.logout_view, name='logout'),

    path('contrats/', contrats.liste, name='contrats-liste'),
    path('contrats/nouveau/', contrats.creer, name='contrats-creer'),
    path('contrats/<int:pk>/', contrats.detail, name='contrats-detail'),
    path('contrats/<int:pk>/modifier/', contrats.modifier, name='contrats-modifier'),
    path('contrats/<int:pk>/sous-contrat/', contrats.ajouter_sous_contrat, name='contrats-sous-contrat'),
    path('contrats/<int:pk>/leases/annuler/', contrats.annuler_leases, name='contrats-annuler-leases'),

    path('types-contrat/', types_contrat.liste, name='types-contrat-liste'),
    path('types-contrat/nouveau/', types_contrat.creer, name='types-contrat-creer'),
    path('types-contrat/<int:pk>/modifier/', types_contrat.modifier, name='types-contrat-modifier'),

    path('leases/', leases.liste, name='leases-liste'),

    path('paiements/', paiements.liste, name='paiements-liste'),
    path('paiements/nouveau/', paiements.creer, name='paiements-creer'),
    path('paiements/<int:pk>/modifier/', paiements.modifier, name='paiements-modifier'),

    path('chauffeurs/', chauffeurs.liste, name='chauffeurs-liste'),
    path('chauffeurs/nouveau/', chauffeurs.creer, name='chauffeurs-creer'),
    path('chauffeurs/<int:pk>/', chauffeurs.detail, name='chauffeurs-detail'),
    path('chauffeurs/<int:pk>/modifier/', chauffeurs.modifier, name='chauffeurs-modifier'),
    path('chauffeurs/<int:pk>/supprimer/', chauffeurs.supprimer, name='chauffeurs-supprimer'),

    path('transactions/', transactions.liste, name='transactions-liste'),
    path('transactions/<int:pk>/', transactions.detail, name='transactions-detail'),
    path(
        'transactions/<int:pk>/preuve-ussd/valider/',
        transactions.valider_preuve_ussd, name='transactions-valider-ussd',
    ),
    path(
        'transactions/<int:pk>/preuve-ussd/rejeter/',
        transactions.rejeter_preuve_ussd, name='transactions-rejeter-ussd',
    ),

    path('proprietaires/', proprietaires.liste, name='proprietaires-liste'),
    path('proprietaires/nouveau/', proprietaires.creer, name='proprietaires-creer'),
    path('proprietaires/<int:pk>/', proprietaires.detail, name='proprietaires-detail'),
    path('proprietaires/<int:pk>/modifier/', proprietaires.modifier, name='proprietaires-modifier'),
    path(
        'proprietaires/<int:pk>/comptes-reception/nouveau/',
        proprietaires.ajouter_compte_reception, name='proprietaires-compte-reception-creer',
    ),
    path(
        'proprietaires/<int:pk>/comptes-reception/<int:compte_pk>/basculer/',
        proprietaires.basculer_compte_reception, name='proprietaires-compte-reception-basculer',
    ),
]
