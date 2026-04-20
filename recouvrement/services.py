import requests
import logging
from django.conf import settings
from core.exceptions import CustomAPIException


logger = logging.getLogger(__name__)


class MobilePaymentService:
    """
    Service dédié à l'intégration avec la passerelle de paiement Mobile Money.
    Gère la création des sessions de checkout (redirection vers la page de paiement).
    """
    @classmethod
    def _get_headers(cls):
        """Récupère et sécurise les en-têtes HTTP requis par le fournisseur."""
        if not settings.PAYMENT_API_KEY:
            raise CustomAPIException(
                dev_msg="PAYMENT_API_KEY is missing in environment variables.",
                resp_code=500,
                status_code=500,
                usr_msg="Configuration du service de paiement manquante. Contactez l'administrateur."
            )
        return {
            "Authorization": f"Api-Key {settings.PAYMENT_API_KEY}",
            "Content-Type": "application/json"
        }

    @classmethod
    def initier_checkout(cls, montant, external_reference, phone_number=None):
        """
        Appelle l'API /create-checkout-session/ du fournisseur.

        Args:
            montant (Decimal/int): La somme à payer.
            external_reference (str): Notre référence interne générée (ex: PR...).
            phone_number (str, optional): Le numéro du client (pré-remplissage).

        Returns:
            dict: La réponse JSON de l'API contenant le 'redirect_url' et le 'transaction_id'.
        """
        # Nettoyage de l'URL de base pour éviter les doubles slashes (//)
        base_url = settings.PAYMENT_API_BASE_URL.rstrip('/')
        url = f"{base_url}/create-checkout-session/"

        payload = {
            "amount": int(montant),
            "external_reference": external_reference,
            "country_code": "CM",
            "success_url": settings.PAYMENT_SUCCESS_URL,
            "cancel_url": ""
        }

        # Le numéro de téléphone n'est pas strictement requis pour le checkout,
        # on l'ajoute au payload uniquement s'il est fourni.
        if phone_number:
            payload["phone_number"] = phone_number

        try:
            # Timeout de 15s : On ne veut pas que notre serveur Django bloque indéfiniment
            # si l'API du fournisseur Mobile Money est en panne.
            response = requests.post(url, json=payload, headers=cls._get_headers(), timeout=15)

            # Déclenche une exception si le statut HTTP est 4xx ou 5xx
            response.raise_for_status()

            return response.json()

        except requests.exceptions.RequestException as e:
            # On loggue l'erreur technique complète pour les développeurs
            logger.error(f"Erreur API Paiement [{url}] pour la ref {external_reference}: {str(e)}")

            # On remonte une erreur claire et propre au client (l'application mobile/web)
            raise CustomAPIException(
                dev_msg=f"Erreur Gateway [{url}]: {str(e)}",
                resp_code=500,
                status_code=502,
                usr_msg="Le service de paiement est temporairement injoignable. Veuillez réessayer."
            )