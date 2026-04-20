# from rest_framework_simplejwt.authentication import JWTAuthentication
# from django.contrib.auth import get_user_model
# from core.errors import ErrorCodes
#
# User = get_user_model()
#
#
# class KeycloakJWTAuthentication(JWTAuthentication):
#
#     def get_user(self, validated_token):
#         from core.exceptions import CustomAPIException
#         keycloak_id = validated_token.get('sub')
#         compte_id = validated_token.get('compte_id')
#
#         # ==========================================
#         # 1. AUTORISATION À LA FRONTIÈRE (EDGE AUTH)
#         # ==========================================
#         # On vérifie si l'utilisateur a des droits AVANT de déranger la base de données.
#         resource_access = validated_token.get('resource_access', {})
#         recouvrement_app = resource_access.get('recouvrement_app', {})
#         roles_keycloak = recouvrement_app.get('roles', [])
#
#         if not roles_keycloak:
#             raise CustomAPIException(
#                 dev_msg=f"User {keycloak_id} has no roles in Keycloak for 'recouvrement_app'.",
#                 resp_code=ErrorCodes.ACCESS_DENIED,
#                 status_code=403,
#                 usr_msg="Accès refusé. Vous n'avez aucun rôle assigné pour l'application de Recouvrement."
#             )
#
#         # ==========================================
#         # 2. VÉRIFICATION DE L'INTÉGRITÉ
#         # ==========================================
#         if not keycloak_id:
#             raise CustomAPIException(
#                 dev_msg="Missing 'sub' claim in Keycloak token.",
#                 resp_code=ErrorCodes.AUTH_INVALID_TOKEN,
#                 status_code=401,
#                 usr_msg="Jeton d'authentification invalide ou corrompu."
#             )
#
#         # ==========================================
#         # 3. EXISTENCE LOCALE (MODE STRICT)
#         # ==========================================
#         try:
#             user = User.objects.get(keycloak_id=keycloak_id)
#
#         except User.DoesNotExist:
#             raise CustomAPIException(
#                 dev_msg=f"User {keycloak_id} authenticated in Keycloak but not registered in local DB.",
#                 resp_code=ErrorCodes.ACCESS_DENIED,
#                 status_code=403,
#                 usr_msg="Accès refusé. Votre profil n'est pas encore enregistré dans la base de données du Recouvrement."
#             )
#
#         # ==========================================
#         # 4. SÉCURITÉ MULTI-TENANT
#         # ==========================================
#         local_compte_id = getattr(user, 'compte_id', None)
#
#         # Si Keycloak fournit un compte_id, il doit correspondre au compte local
#         if compte_id and local_compte_id and str(local_compte_id) != str(compte_id):
#             raise CustomAPIException(
#                 dev_msg=f"Tenant mismatch. Token says {compte_id}, Local DB says {local_compte_id}.",
#                 resp_code=ErrorCodes.ACCESS_DENIED,
#                 status_code=403,
#                 usr_msg="Incohérence de sécurité détectée sur votre compte. Accès bloqué."
#             )
#
#         # ==========================================
#         # 5. STATUT D'ACTIVATION
#         # ==========================================
#         if not user.is_active:
#             raise CustomAPIException(
#                 dev_msg=f"Local user {user.id} is marked as inactive.",
#                 resp_code=ErrorCodes.USER_INACTIVE,
#                 status_code=403,
#                 usr_msg="Votre accès à cette application a été désactivé par un administrateur."
#             )
#
#         return user


from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.auth import get_user_model
from core.errors import ErrorCodes

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
                dev_msg=f"User {keycloak_id} has no roles in Keycloak for 'recouvrement_app'.",
                resp_code=ErrorCodes.ACCESS_DENIED,
                status_code=403,
                usr_msg="Accès refusé. Vous n'avez aucun rôle assigné pour l'application de Recouvrement."
            )

        # ==========================================
        # 2. VÉRIFICATION DE L'INTÉGRITÉ
        # ==========================================
        if not keycloak_id:
            raise CustomAPIException(
                dev_msg="Missing 'sub' claim in Keycloak token.",
                resp_code=ErrorCodes.AUTH_INVALID_TOKEN,
                status_code=401,
                usr_msg="Jeton d'authentification invalide ou corrompu."
            )

        # ==========================================
        # 3. EXISTENCE LOCALE OU CRÉATION JIT
        # ==========================================
        try:
            user = User.objects.get(keycloak_id=keycloak_id)

            # (Optionnel) On synchronise les rôles à chaque connexion
            # pour s'assurer qu'ils sont toujours à jour avec Keycloak
            self.sync_roles(user, roles_keycloak, compte_id)

        except User.DoesNotExist:
            # L'utilisateur n'existe pas : on le provisionne (JIT)
            if not compte_id:
                raise CustomAPIException(
                    dev_msg="Cannot JIT provision user without 'compte_id'.",
                    resp_code=ErrorCodes.INVALID_PAYLOAD,
                    status_code=403,
                    usr_msg="Configuration incomplète : Votre profil n'est pas rattaché à une entreprise (compte_id manquant dans Keycloak)."
                )

            # Extraction de l'email
            email = validated_token.get('email', '')

            # Extraction et construction du nom complet
            prenom = validated_token.get('given_name', '')
            nom = validated_token.get('family_name', '')
            nom_complet = f"{prenom} {nom}".strip()

            # Fallback de sécurité : si prenom et nom sont vides, on prend le champ 'name' global
            if not nom_complet:
                nom_complet = validated_token.get('name', '')

            # Création silencieuse dans la base locale
            user = User.objects.create(
                keycloak_id=keycloak_id,
                compte_id=compte_id,
                email=email,
                nom_complet=nom_complet,
                is_active=True
            )

            # Synchronisation de ses rôles immédiatement après la création
            self.sync_roles(user, roles_keycloak, compte_id)

        # ==========================================
        # 4. SÉCURITÉ MULTI-TENANT (Double Check)
        # ==========================================
        local_compte_id = getattr(user, 'compte_id', None)

        if compte_id and local_compte_id and str(local_compte_id) != str(compte_id):
            raise CustomAPIException(
                dev_msg=f"Tenant mismatch. Token says {compte_id}, Local DB says {local_compte_id}.",
                resp_code=ErrorCodes.ACCESS_DENIED,
                status_code=403,
                usr_msg="Incohérence de sécurité détectée sur votre compte. Accès bloqué."
            )

        # ==========================================
        # 5. STATUT D'ACTIVATION
        # ==========================================
        if getattr(user, 'is_active', True) is False:
            raise CustomAPIException(
                dev_msg=f"Local user {user.id} is marked as inactive.",
                resp_code=ErrorCodes.USER_INACTIVE,
                status_code=403,
                usr_msg="Votre accès à cette application a été désactivé par un administrateur."
            )

        return user

    def sync_roles(self, user, keycloak_roles, compte_id):
        """
        Méthode interne pour associer les rôles Keycloak aux rôles locaux.
        L'import se fait ici pour éviter les erreurs d'import circulaire au démarrage de Django.
        """
        from accounts.models import Role, CustomUserRole

        if not keycloak_roles:
            return

        # On cherche les rôles locaux qui ont le même slug que ceux du token
        roles_locaux = Role.objects.filter(slug__in=keycloak_roles)

        for role in roles_locaux:
            CustomUserRole.objects.get_or_create(
                user=user,
                role=role,
                compte_id=compte_id,
                defaults={
                    'actif': True,
                    'principal': False  # Peut être ajusté selon tes règles métiers
                }
            )