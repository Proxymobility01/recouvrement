class ErrorCodes:
    # --- ERREURS GLOBALES ---
    SYSTEM_ERROR = 50000
    BAD_REQUEST = 40000
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    INVALID_PAYLOAD = 40001

    # --- ERREURS SPÉCIFIQUES ---
    AUTH_TOKEN_EXPIRED = 40104
    CONTRACT_ALREADY_SETTLED = 40203
    PAYMENT_ALREADY_PROCESSED = 40206
    PAYMENT_EXCEEDS_REMAINING = 40213
    MOBILE_MONEY_FAILED = 40207


# Messages destinés à l'utilisateur final (Le Front-End piochera ici)
ERROR_MESSAGES = {
    # Global
    ErrorCodes.SYSTEM_ERROR: "Erreur interne serveur.",
    ErrorCodes.BAD_REQUEST: "Requête invalide.",
    ErrorCodes.UNAUTHORIZED: "Authentification requise.",
    ErrorCodes.FORBIDDEN: "Action non autorisée.",
    ErrorCodes.NOT_FOUND: "Ressource introuvable.",
    ErrorCodes.INVALID_PAYLOAD: "Données invalides.",

    # Spécifique
    ErrorCodes.AUTH_TOKEN_EXPIRED: "Session expirée.",
    ErrorCodes.CONTRACT_ALREADY_SETTLED: "Contrat déjà soldé.",
    ErrorCodes.PAYMENT_ALREADY_PROCESSED: "Transaction déjà validée.",
    ErrorCodes.PAYMENT_EXCEEDS_REMAINING: "Le montant dépasse le reste à payer.",
    ErrorCodes.MOBILE_MONEY_FAILED: "Échec de la transaction.",
}