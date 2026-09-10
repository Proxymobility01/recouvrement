import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import login as django_login, logout as django_logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from keycloak import KeycloakOpenID
from keycloak.exceptions import KeycloakError
from rest_framework_simplejwt.exceptions import InvalidToken

from core.authentication import (
    KeycloakJWTAuthentication,
    ProvisioningError,
    provisionner_utilisateur_depuis_token,
)

logger = logging.getLogger(__name__)


def _client_keycloak():
    return KeycloakOpenID(
        server_url=settings.KEYCLOAK_URL,
        realm_name=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_CLIENT_ID,
        client_secret_key=settings.KEYCLOAK_CLIENT_SECRET,
    )


def _rediriger_avec_erreur(message):
    return redirect(f"{reverse('frontend:login')}?{urlencode({'erreur': message})}")


def _generer_pkce():
    """
    Couple (code_verifier, code_challenge) pour PKCE (RFC 7636), S256.
    Le client Keycloak 'recouvrement_app' l'exige (Missing parameter:
    code_challenge_method) même en client confidentiel avec secret.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    return verifier, challenge


def login_view(request):
    """
    GET /partenaire/login/

    Sans `?erreur=`, redirige immédiatement vers Keycloak (flux Authorization
    Code + PKCE). Avec `?erreur=`, affiche le message au lieu de rebondir en
    boucle sur un callback qui vient d'échouer.
    """
    erreur = request.GET.get('erreur')
    if erreur:
        return render(request, 'frontend/login.html', {'erreur': erreur})

    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _generer_pkce()

    suivant = request.GET.get('next', '')
    if suivant and not url_has_allowed_host_and_scheme(
        suivant, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        suivant = ''

    request.session['keycloak_state'] = state
    request.session['keycloak_next'] = suivant
    request.session['keycloak_code_verifier'] = code_verifier

    redirect_uri = request.build_absolute_uri(reverse('frontend:login-callback'))
    url_autorisation = _client_keycloak().auth_url(
        redirect_uri=redirect_uri,
        scope='openid',
        state=state,
        code_challenge=code_challenge,
        code_challenge_method='S256',
    )
    return redirect(url_autorisation)


def login_callback_view(request):
    """GET /partenaire/login/callback/ — retour de Keycloak après authentification."""
    erreur_keycloak = request.GET.get('error')
    if erreur_keycloak:
        description = request.GET.get('error_description', erreur_keycloak)
        logger.warning("Keycloak a refusé la demande d'autorisation : %s", description)
        return _rediriger_avec_erreur(f"Connexion refusée par Keycloak : {description}")

    state_recu = request.GET.get('state')
    state_attendu = request.session.pop('keycloak_state', None)
    suivant = request.session.pop('keycloak_next', '')
    code_verifier = request.session.pop('keycloak_code_verifier', None)
    code = request.GET.get('code')

    if not code or not state_recu or not state_attendu or state_recu != state_attendu:
        return _rediriger_avec_erreur(
            "Connexion invalide ou expirée, veuillez réessayer."
        )

    redirect_uri = request.build_absolute_uri(reverse('frontend:login-callback'))

    try:
        jetons = _client_keycloak().token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        # Réutilise exactement la même validation cryptographique (cache
        # JWKS Redis inclus) que l'authentification JWT de l'API.
        validated_token = KeycloakJWTAuthentication().get_validated_token(
            jetons['access_token']
        )
        # Réutilise exactement le même provisioning JIT + synchronisation
        # des rôles que l'API, pour ne jamais diverger entre les deux.
        user = provisionner_utilisateur_depuis_token(validated_token)
    except (KeycloakError, InvalidToken, ProvisioningError) as exc:
        logger.warning("Échec de connexion SSO Keycloak : %s", exc)
        return _rediriger_avec_erreur(
            "Échec de la connexion. Contactez votre administrateur."
        )

    # Provisioning JIT via Keycloak : pas de vérification de mot de passe,
    # donc pas d'authenticate() — on désigne explicitement le backend.
    user.backend = 'django.contrib.auth.backends.ModelBackend'
    django_login(request, user)
    return redirect(suivant or settings.LOGIN_REDIRECT_URL)


def logout_view(request):
    """POST/GET /partenaire/logout/ — déconnexion locale (session Django uniquement)."""
    django_logout(request)
    return redirect('frontend:login')
