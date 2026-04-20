class ErrorCodes:
    # ==========================================
    # MODULE 00 : SYSTEME (Générique)
    # ==========================================
    SYSTEM_ERROR = 50001  # 5 (Serveur) + 00 (System) + 01
    INVALID_PAYLOAD = 40001  # 4 (Client)  + 00 (System) + 01
    ENDPOINT_NOT_FOUND = 40002  # 4 (Client)  + 00 (System) + 02

    # ==========================================
    # MODULE 01 : ACCOUNTS (Identité locale & Multi-tenant)
    # ==========================================
    TENANT_NOT_FOUND = 40101
    AUTH_MISSING_TOKEN = 40102
    AUTH_INVALID_TOKEN = 40103
    AUTH_TOKEN_EXPIRED = 40104
    ACCESS_DENIED = 40105
    USER_INACTIVE = 40106  # Utile si l'agent a été désactivé dans la table locale

    # ==========================================
    # MODULE 02 : RECOUVREMENT (Logique Métier)
    # ==========================================
    # -> Contrats
    CONTRACT_NOT_FOUND = 40201
    CONTRACT_SUSPENDED = 40202
    CONTRACT_ALREADY_SETTLED = 40203  # Contrat déjà soldé

    # -> Paiements
    PAYMENT_INVALID_AMOUNT = 40204  # Ex: montant payé > montant restant
    PAYMENT_ALREADY_PROCESSED = 40205  # Évite les doubles paiements
    MOBILE_MONEY_FAILED = 40206  # Échec de l'API externe (MTN/Orange)


# Dictionnaire centralisé pour les messages utilisateurs (UI)
ERROR_MESSAGES = {
    # Système
    ErrorCodes.SYSTEM_ERROR: "Une erreur interne est survenue sur nos serveurs.",
    ErrorCodes.INVALID_PAYLOAD: "Les données envoyées sont invalides ou incomplètes.",
    ErrorCodes.ENDPOINT_NOT_FOUND: "La ressource demandée n'existe pas.",

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

    ErrorCodes.PAYMENT_INVALID_AMOUNT: "Le montant du paiement est invalide.",
    ErrorCodes.PAYMENT_ALREADY_PROCESSED: "Cette transaction a déjà été validée.",
    ErrorCodes.MOBILE_MONEY_FAILED: "La transaction Mobile Money a échoué. Veuillez réessayer."
}