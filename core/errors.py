class ErrorCodes:
    # --- MODULE 00 : SYSTEME ---
    SYSTEM_ERROR = 50001
    INVALID_PAYLOAD = 40001
    ENDPOINT_NOT_FOUND = 40002

    # --- MODULE 01 : ACCOUNTS ---
    TENANT_NOT_FOUND = 40101
    AUTH_MISSING_TOKEN = 40102
    AUTH_INVALID_TOKEN = 40103
    AUTH_TOKEN_EXPIRED = 40104
    ACCESS_DENIED = 40105
    USER_INACTIVE = 40106

    AUTH_NO_APP_ROLES = 40107
    AUTH_MISSING_TENANT_ID = 40108
    AUTH_TENANT_MISMATCH = 40109

    # --- MODULE 02 : RECOUVREMENT ---
    CONTRACT_NOT_FOUND = 40201
    CONTRACT_SUSPENDED = 40202
    CONTRACT_ALREADY_SETTLED = 40203
    CONTRACT_DELETE_FORBIDDEN = 40204

    PAYMENT_INVALID_AMOUNT = 40205
    PAYMENT_ALREADY_PROCESSED = 40206
    MOBILE_MONEY_FAILED = 40207

    LEASE_DELETE_FORBIDDEN = 40208

    TENANT_SUPERUSER_MISSING_ID = 40110
    TENANT_INVALID_ID_FORMAT = 40111

    LEASE_NOT_FOUND_OR_DENIED = 40211
    PAYMENT_EXCEEDS_REMAINING = 40213
    PAYMENT_DUPLICATE_LEASES = 40214
    PAYMENT_MIXED_CONTRACTS = 40215
    PAYMENT_CHRONOLOGY_VIOLATION = 40216


# 1. Messages destinés à l'utilisateur final (Front-End / Toast)
ERROR_MESSAGES_USR = {
    # Système
    ErrorCodes.SYSTEM_ERROR: "Une erreur interne est survenue sur nos serveurs.",
    ErrorCodes.INVALID_PAYLOAD: "Les données envoyées sont invalides ou incomplètes.",
    ErrorCodes.ENDPOINT_NOT_FOUND: "La ressource demandée n'existe pas.",

    ErrorCodes.AUTH_NO_APP_ROLES: "Accès refusé. Vous n'avez aucun rôle assigné pour l'application de Recouvrement.",
    ErrorCodes.AUTH_MISSING_TENANT_ID: "Configuration incomplète : Votre profil n'est pas rattaché à une entreprise.",
    ErrorCodes.AUTH_TENANT_MISMATCH: "Incohérence de sécurité détectée sur votre compte. Accès bloqué.",

    ErrorCodes.LEASE_NOT_FOUND_OR_DENIED: "L'échéance demandée est introuvable ou vous n'avez pas l'autorisation d'y accéder.",
    ErrorCodes.PAYMENT_EXCEEDS_REMAINING: "Le montant saisi dépasse le reste à payer pour cette échéance.",
    ErrorCodes.PAYMENT_DUPLICATE_LEASES: "Vous avez sélectionné plusieurs fois la même échéance dans votre panier.",
    ErrorCodes.PAYMENT_MIXED_CONTRACTS: "Toutes les échéances payées en une fois doivent appartenir au même groupe de contrats (ex: la moto et ses accessoires).",
    ErrorCodes.PAYMENT_CHRONOLOGY_VIOLATION: "Paiement refusé : Vous devez d'abord régler les échéances les plus anciennes de vos contrats.",

    # Accounts
    ErrorCodes.TENANT_NOT_FOUND: "Compte introuvable ou vous n'y avez pas accès.",
    ErrorCodes.AUTH_MISSING_TOKEN: "Authentification requise. Aucun token fourni.",
    ErrorCodes.AUTH_INVALID_TOKEN: "Votre session est invalide.",
    ErrorCodes.AUTH_TOKEN_EXPIRED: "Votre session a expiré. Veuillez vous reconnecter.",
    ErrorCodes.ACCESS_DENIED: "Vous n'avez pas les permissions nécessaires pour cette action.",
    ErrorCodes.USER_INACTIVE: "Votre accès à cette application a été désactivé.",

    # Recouvrement
    ErrorCodes.CONTRACT_NOT_FOUND: "Ce contrat n'existe pas ou n'appartient pas à votre compte.",
    ErrorCodes.CONTRACT_SUSPENDED: "Action impossible : ce contrat est actuellement suspendu.",
    ErrorCodes.CONTRACT_ALREADY_SETTLED: "Ce contrat est déjà totalement soldé.",
    ErrorCodes.CONTRACT_DELETE_FORBIDDEN: "Suppression interdite. Modifiez le statut en 'Annulé' ou 'Soldé'.",

    ErrorCodes.PAYMENT_INVALID_AMOUNT: "Le montant du paiement est invalide.",
    ErrorCodes.PAYMENT_ALREADY_PROCESSED: "Cette transaction a déjà été validée.",
    ErrorCodes.MOBILE_MONEY_FAILED: "La transaction Mobile Money a échoué. Veuillez réessayer.",
    ErrorCodes.LEASE_DELETE_FORBIDDEN: "Suppression interdite. Une échéance comptable ne peut pas être effacée.",

    ErrorCodes.TENANT_SUPERUSER_MISSING_ID: "En tant que Super Administrateur, précisez le compte_id cible pour cette création.",
    ErrorCodes.TENANT_INVALID_ID_FORMAT: "Le paramètre compte_id doit être un nombre entier valide.",
}

# 2. Messages destinés aux développeurs (Logs / Debug)
ERROR_MESSAGES_DEV = {
    # Système
    ErrorCodes.SYSTEM_ERROR: "Erreur interne non gérée (Exception Python).",
    ErrorCodes.INVALID_PAYLOAD: "Le payload JSON ne correspond pas au Serializer.",
    ErrorCodes.ENDPOINT_NOT_FOUND: "L'URL demandée n'existe pas dans le routeur.",

    ErrorCodes.LEASE_NOT_FOUND_OR_DENIED: "Lease inexistant ou non rattaché au compte/chauffeur actuel.",
    ErrorCodes.PAYMENT_EXCEEDS_REMAINING: "Le montant fourni > (montant_attendu - montant_paye).",
    ErrorCodes.PAYMENT_DUPLICATE_LEASES: "Présence de doublons dans la liste des lease_id.",
    ErrorCodes.PAYMENT_MIXED_CONTRACTS: "Les leases soumis n'ont pas tous le même parent_id final.",
    ErrorCodes.PAYMENT_CHRONOLOGY_VIOLATION: "Violation de la règle chronologique. Des leases antérieurs impayés existent.",

    # Accounts
    ErrorCodes.TENANT_NOT_FOUND: "Le Tenant ID fourni ne correspond à aucun compte existant.",
    ErrorCodes.AUTH_MISSING_TOKEN: "Aucun Bearer token dans le header Authorization.",
    ErrorCodes.AUTH_INVALID_TOKEN: "Signature JWT invalide ou utilisateur introuvable.",
    ErrorCodes.AUTH_TOKEN_EXPIRED: "Le token JWT est arrivé à expiration.",
    ErrorCodes.ACCESS_DENIED: "Action bloquée par les PermissionClasses de DRF.",
    ErrorCodes.USER_INACTIVE: "L'attribut is_active de l'utilisateur est False.",

    ErrorCodes.AUTH_NO_APP_ROLES: "L'utilisateur n'a aucun rôle dans le client 'recouvrement_app' sur Keycloak.",
    ErrorCodes.AUTH_MISSING_TENANT_ID: "Impossible de provisionner (JIT) l'utilisateur : le claim 'compte_id' est absent du token Keycloak.",
    ErrorCodes.AUTH_TENANT_MISMATCH: "Incohérence critique : le compte_id du token Keycloak diffère de celui de la base locale.",

    # Recouvrement
    ErrorCodes.CONTRACT_NOT_FOUND: "Requête sur un ID de contrat inexistant ou hors du queryset autorisé.",
    ErrorCodes.CONTRACT_SUSPENDED: "Tentative de modification/paiement sur un contrat ayant le statut SUSPENDU.",
    ErrorCodes.CONTRACT_ALREADY_SETTLED: "Tentative de paiement sur un contrat ayant le statut SOLDE.",
    ErrorCodes.CONTRACT_DELETE_FORBIDDEN: "Hard-delete bloqué sur le ViewSet (Règle comptable).",

    ErrorCodes.PAYMENT_INVALID_AMOUNT: "Le montant soumis est supérieur au montant restant du contrat.",
    ErrorCodes.PAYMENT_ALREADY_PROCESSED: "Un paiement avec cette référence de transaction existe déjà.",
    ErrorCodes.MOBILE_MONEY_FAILED: "Timeout ou réponse 400/500 de l'API agrégateur de paiement.",
    ErrorCodes.LEASE_DELETE_FORBIDDEN: "Hard-delete bloqué sur l'échéance (Lease). Règle d'intégrité financière.",
    ErrorCodes.TENANT_SUPERUSER_MISSING_ID: "Le payload POST d'un superuser doit obligatoirement inclure un 'compte_id'.",
    ErrorCodes.TENANT_INVALID_ID_FORMAT: "Le champ 'compte_id' fourni ne peut pas être converti en entier (Integer).",
}