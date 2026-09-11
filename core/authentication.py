import jwt
import requests
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
from core.errors import ErrorCodes

logger = logging.getLogger(__name__)
User = get_user_model()


class KeycloakJWTAuthentication(JWTAuthentication):
    """
    Authentification JWT avec mise en cache ultra-rapide des clés publiques Keycloak
    et Provisioning Just-In-Time (JIT).
    """

    def get_jwks(self, force_refresh=False):
        """
        🚀 GESTION DU CACHE : Récupère les clés publiques depuis Redis.
        Utilise l'URL dynamique configurée dans les settings.
        """
        cache_key = "keycloak_public_keys"

        if force_refresh:
            cache.delete(cache_key)
            logger.info("🔄 Cache JWKS Keycloak invalidé manuellement (Rotation des clés suspectée).")
        else:
            cached_jwks = cache.get(cache_key)
            if cached_jwks:
                return cached_jwks

        # 🚀 Utilisation de la variable dynamique des settings
        jwks_url = settings.KEYCLOAK_JWKS_URL

        try:
            logger.info(f"[Auth] 🌐 Téléchargement des clés publiques depuis {jwks_url} (Cache Miss)...")
            response = requests.get(jwks_url, timeout=5)
            response.raise_for_status()
            jwks = response.json()

            # Sauvegarde dans Redis pour 24 heures (86400 secondes)
            cache.set(cache_key, jwks, 86400)
            return jwks

        except requests.RequestException as e:
            logger.exception("Erreur réseau lors de la récupération des certificats Keycloak.")
            raise AuthenticationFailed("Le serveur d'authentification est temporairement injoignable.")

    def get_validated_token(self, raw_token):
        """
        Intercepte la validation de SimpleJWT pour utiliser notre cache Redis.
        Remplace la requête HTTP de 724ms par un calcul cryptographique local de 2ms.
        """
        try:
            # 1. Lecture de l'en-tête du token
            unverified_header = jwt.get_unverified_header(raw_token)
            kid = unverified_header.get('kid')

            if not kid:
                raise InvalidToken("Token malformé : identifiant de clé ('kid') manquant dans le header.")

            # 2. Récupération des clés publiques
            jwks = self.get_jwks()

            # 3. Recherche de la clé correspondante
            rsa_key = next((key for key in jwks.get('keys', []) if key['kid'] == kid), None)

            # 🚨 SÉCURITÉ : Rotation des clés Keycloak
            if not rsa_key:
                logger.warning(f"Clé publique (kid: {kid}) introuvable. Forçage du rafraîchissement du cache...")
                jwks = self.get_jwks(force_refresh=True)
                rsa_key = next((key for key in jwks.get('keys', []) if key['kid'] == kid), None)

                if not rsa_key:
                    raise InvalidToken("Signature de sécurité invalide ou clé publique obsolète.")

            # 4. Reconstruction de la clé publique
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(rsa_key)

            # 5. Validation cryptographique mathématique locale
            # On utilise dynamiquement les règles de ton dictionnaire SIMPLE_JWT
            decoded_payload = jwt.decode(
                raw_token,
                key=public_key,
                algorithms=[settings.SIMPLE_JWT.get('ALGORITHM', 'RS256')],
                audience=settings.SIMPLE_JWT.get('AUDIENCE'),
                issuer=settings.SIMPLE_JWT.get('ISSUER'),
                # Tolère un léger décalage d'horloge avec le serveur Keycloak
                # (souvent une machine distincte) sur exp/iat/nbf : sans ça,
                # la moindre dérive fait échouer 100% des connexions avec
                # "The token is not yet valid (iat)".
                leeway=settings.SIMPLE_JWT.get('LEEWAY', 0),
                options={
                    "verify_aud": True if settings.SIMPLE_JWT.get('AUDIENCE') else False,
                    "verify_iss": True if settings.SIMPLE_JWT.get('ISSUER') else False,
                }
            )

            # 6. Formatage pour Django REST Framework
            token = AccessToken(raw_token, verify=False)
            token.payload = decoded_payload
            return token

        except jwt.ExpiredSignatureError:
            raise InvalidToken("Le token a expiré. Veuillez vous reconnecter.")
        except jwt.DecodeError:
            raise InvalidToken("Le token est illisible ou corrompu.")
        except jwt.InvalidTokenError as e:
            raise InvalidToken(f"Échec cryptographique de la validation du token : {str(e)}")

    def get_user(self, validated_token):
        from core.exceptions import CustomAPIException

        try:
            return provisionner_utilisateur_depuis_token(validated_token)
        except ProvisioningError as exc:
            raise CustomAPIException(
                resp_code=exc.resp_code,
                status_code=exc.status_code,
                dev_message=exc.dev_message,
            ) from exc

    def sync_roles(self, user, keycloak_roles, compte_id):
        """Conservé pour compatibilité : délègue à la fonction partagée."""
        synchroniser_roles(user, keycloak_roles, compte_id)


class ProvisioningError(Exception):
    """
    Échec de provisioning/synchronisation d'un utilisateur à partir d'un
    token Keycloak validé. Volontairement indépendante de DRF pour rester
    utilisable depuis une vue Django classique (ex: callback de login SSO).
    """

    def __init__(self, resp_code, status_code, dev_message):
        self.resp_code = resp_code
        self.status_code = status_code
        self.dev_message = dev_message
        super().__init__(dev_message)


def provisionner_utilisateur_depuis_token(validated_token):
    """
    Provisioning JIT + synchronisation des rôles à partir d'un token Keycloak
    déjà validé cryptographiquement.

    Logique partagée par l'authentification DRF (`KeycloakJWTAuthentication.
    get_user`) et par le callback de login SSO du frontend partenaire : les
    deux doivent créer/retrouver exactement le même `CustomUser` de la même
    façon, sans dupliquer cette logique.
    """
    keycloak_id = validated_token.get('sub')
    compte_id = validated_token.get('compte_id')

    # ==========================================
    # 1. AUTORISATION À LA FRONTIÈRE (EDGE AUTH)
    # ==========================================
    resource_access = validated_token.get('resource_access', {})
    recouvrement_app = resource_access.get('recouvrement_app', {})
    roles_keycloak = recouvrement_app.get('roles', [])

    if not roles_keycloak:
        raise ProvisioningError(
            resp_code=ErrorCodes.FORBIDDEN,
            status_code=403,
            dev_message=f"Aucun rôle 'recouvrement_app' détecté pour Keycloak ID: {keycloak_id}"
        )

    # ==========================================
    # 2. VÉRIFICATION DE L'INTÉGRITÉ
    # ==========================================
    if not keycloak_id:
        raise ProvisioningError(
            resp_code=ErrorCodes.UNAUTHORIZED,
            status_code=401,
            dev_message="Claim 'sub' (Keycloak ID) introuvable dans le token JWT."
        )

    # ==========================================
    # 3. EXISTENCE LOCALE OU CRÉATION JIT
    # ==========================================
    try:
        user = User.objects.get(keycloak_id=keycloak_id)

        # 🚀 SÉCURITÉ MULTI-TENANT : Vérification AVANT de toucher à la BDD
        local_compte_id = getattr(user, 'compte_id', None)

        if compte_id and local_compte_id and str(local_compte_id) != str(compte_id):
            raise ProvisioningError(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Conflit de sécurité Tenant: Token={compte_id} vs Local={local_compte_id}"
            )

        # On synchronise les rôles en utilisant le compte_id local validé
        synchroniser_roles(user, roles_keycloak, user.compte_id)

    except User.DoesNotExist:
        logger.info("Provisioning JIT - Keycloak ID: %s / compte_id: %s", keycloak_id, compte_id)

        if not compte_id:
            raise ProvisioningError(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Provisioning échoué pour Keycloak ID {keycloak_id} : 'compte_id' manquant dans le JWT."
            )

        # Extraction des données
        email = validated_token.get('email', '')
        prenom = validated_token.get('given_name', '')
        nom = validated_token.get('family_name', '')
        nom_complet = f"{prenom} {nom}".strip()

        # Fallback de sécurité
        if not nom_complet:
            nom_complet = validated_token.get('name', '')

        # Création silencieuse
        user = User.objects.create(
            keycloak_id=keycloak_id,
            compte_id=compte_id,
            email=email,
            nom_complet=nom_complet,
            is_active=True
        )

        # Synchronisation des rôles
        synchroniser_roles(user, roles_keycloak, user.compte_id)

    # ==========================================
    # 4. STATUT D'ACTIVATION
    # ==========================================
    if getattr(user, 'is_active', True) is False:
        raise ProvisioningError(
            resp_code=ErrorCodes.FORBIDDEN,
            status_code=403,
            dev_message=f"Compte inactif en BDD locale pour User ID: {user.id}"
        )

    import sentry_sdk

    # On envoie l'identité à Sentry maintenant que le JWT est validé
    sentry_sdk.set_user({
        "id": user.id,
        "email": user.email,
        "keycloak_id": getattr(user, 'keycloak_id', None)
    })

    # On ajoute le Tag Multi-Tenant
    local_compte_id = getattr(user, 'compte_id', None)
    if local_compte_id:
        sentry_sdk.set_tag("compte_id", local_compte_id)

    return user


def synchroniser_roles(user, keycloak_roles, compte_id):
    """Associe les rôles Keycloak du token aux rôles locaux (`Role`/`CustomUserRole`)."""
    from accounts.models import Role, CustomUserRole

    if not keycloak_roles or not compte_id:
        logger.warning(
            "synchroniser_roles ignoré - user: %s / compte_id: %s / roles: %s",
            user.id, compte_id, keycloak_roles
        )
        return

    try:
        compte_id_int = int(compte_id)
    except (TypeError, ValueError):
        logger.error(f"Le compte_id JWT n'est pas un nombre valide : {compte_id}")
        return

    # On cherche les rôles locaux
    roles_locaux = Role.objects.filter(slug__in=keycloak_roles)

    for role in roles_locaux:
        obj, created = CustomUserRole.objects.get_or_create(
            user=user,
            role=role,
            compte_id=compte_id_int,
            defaults={'actif': True, 'principal': False}
        )
        if created:
            logger.info(
                "Rôle '%s' assigné à user %s (compte %s)",
                role.slug, user.id, compte_id_int
            )