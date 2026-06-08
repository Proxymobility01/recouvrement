import logging

from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.auth import get_user_model
from core.errors import ErrorCodes

logger = logging.getLogger(__name__)
User = get_user_model()


class KeycloakJWTAuthentication(JWTAuthentication):
    """
    Authentification JWT (Mode Just-In-Time Provisioning).
    Crée l'utilisateur silencieusement s'il possède les droits dans Keycloak
    mais n'existe pas encore dans la base locale.
    """

    def get_user(self, validated_token):
        from core.exceptions import CustomAPIException

        keycloak_id = validated_token.get('sub')
        compte_id = validated_token.get('compte_id')

        # ==========================================
        # 1. AUTORISATION À LA FRONTIÈRE (EDGE AUTH)
        # ==========================================
        resource_access = validated_token.get('resource_access', {})
        recouvrement_app = resource_access.get('recouvrement_app', {})
        roles_keycloak = recouvrement_app.get('roles', [])

        if not roles_keycloak:
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Aucun rôle 'recouvrement_app' détecté pour Keycloak ID: {keycloak_id}"
            )

        # ==========================================
        # 2. VÉRIFICATION DE L'INTÉGRITÉ
        # ==========================================
        if not keycloak_id:
            raise CustomAPIException(
                resp_code=ErrorCodes.UNAUTHORIZED,
                status_code=401,
                dev_message="Claim 'sub' (Keycloak ID) introuvable dans le token JWT."
            )

        # ==========================================
        # 3. EXISTENCE LOCALE OU CRÉATION JIT
        # ==========================================
        try:
            user = User.objects.get(keycloak_id=keycloak_id)
            # On synchronise les rôles à chaque connexion
            self.sync_roles(user, roles_keycloak, compte_id)

        except User.DoesNotExist:
            logger.info("Provisioning JIT - Keycloak ID: %s / compte_id: %s", keycloak_id, compte_id)

            if not compte_id:
                raise CustomAPIException(
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
            self.sync_roles(user, roles_keycloak, compte_id)

        # ==========================================
        # 4. SÉCURITÉ MULTI-TENANT (Double Check)
        # ==========================================
        local_compte_id = getattr(user, 'compte_id', None)

        if compte_id and local_compte_id and str(local_compte_id) != str(compte_id):
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Conflit de sécurité Tenant: Token={compte_id} vs Local={local_compte_id}"
            )

        # ==========================================
        # 5. STATUT D'ACTIVATION
        # ==========================================
        if getattr(user, 'is_active', True) is False:
            raise CustomAPIException(
                resp_code=ErrorCodes.FORBIDDEN,
                status_code=403,
                dev_message=f"Compte inactif en BDD locale pour User ID: {user.id}"
            )

        return user

    def sync_roles(self, user, keycloak_roles, compte_id):
        """
        Méthode interne pour associer les rôles Keycloak aux rôles locaux.
        """
        from accounts.models import Role, CustomUserRole

        if not keycloak_roles or not compte_id:
            logger.warning(
                "sync_roles ignoré - user: %s / compte_id: %s / roles: %s",
                user.id, compte_id, keycloak_roles
            )
            return

        try:
            compte_id_int = int(compte_id)
        except (TypeError, ValueError):
            logger.exception(f"Le compte_id JWT n'est pas un nombre valide : {compte_id}")
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
                    role.slug, user.id, compte_id
                )