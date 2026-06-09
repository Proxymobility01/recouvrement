import requests
import logging
from django.conf import settings
from django.core.cache import cache

from accounts.models import ConfigPaiement
from core.errors import ErrorCodes  # 🚀 Import des codes d'erreur
from core.exceptions import CustomAPIException
from core.utils import format_phone_cm

logger = logging.getLogger(__name__)


class PaymentService:
    """
    Service Multi-Tenant optimisé pour les paiements Mobile Money.
    Met en cache la configuration (API Key, URLs) pour soulager la base de données,
    et enchaîne les requêtes dans une session HTTP unique (Keep-Alive).
    """

    @classmethod
    def _get_config(cls, compte_id, force_refresh=False):
        """
        🚀 GESTION DU CACHE : Tente de récupérer la configuration depuis la RAM.
        S'il n'y est pas, interroge la BDD et le met en cache pour 24h.
        """
        cache_key = f"credentials_{compte_id}"

        if force_refresh:
            cache.delete(cache_key)
            logger.info(f"🔄 Cache de configuration invalidé manuellement pour le compte {compte_id}.")
        else:
            cached_config = cache.get(cache_key)
            if cached_config:
                return cached_config

        # --- Si non trouvé dans le cache, on attaque la base de données ---
        config = ConfigPaiement.objects.filter(compte_id=compte_id).first()

        # 🚀 SÉCURITÉ MULTI-TENANT STRICTE : Pas de fallback. On bloque si ce n'est pas configuré.
        if not config:
            logger.error(f"[PayGate] Tentative de paiement échouée : compte_id {compte_id} sans configuration.")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=400,  # 400 Bad Request est plus adapté ici car c'est un pré-requis manquant
                dev_message=f"Le partenaire (compte_id: {compte_id}) n'a pas configuré ses identifiants de paiement."
            )

        # On extrait uniquement ce qui doit aller dans le cache Redis
        config_data = {
            "api_key": config.api_key,
            "base_url": config.base_url.rstrip('/'),
            "success_url": config.success_url
        }

        # 🚀 Sauvegarde du dictionnaire dans le cache pour 24 heures (86400 secondes)
        cache.set(cache_key, config_data, 86400)

        return config_data

    @classmethod
    def traiter_paiement_complet(cls, compte_id, montant, external_reference, phone_number):
        """
        ⚡ MÉCANIQUE UNIFIÉE : Récupère la config (via cache), ouvre une seule session HTTP,
        crée la session de paiement avec l'API Key et déclenche instantanément la collecte.
        """
        config = cls._get_config(compte_id)
        base_url = config['base_url']
        local_phone = format_phone_cm(phone_number)

        # OUVERTURE DE LA SESSION UNIQUE (Keep-Alive)
        with requests.Session() as session:
            headers = {
                "Authorization": f"Api-Key {config['api_key']}",
                "Content-Type": "application/json"
            }

            checkout_payload = {
                "amount": int(montant),
                "phone_number": local_phone,
                "external_reference": external_reference,
                "currency": "XAF",
                "success_url": config['success_url'],
                "cancel_url": ""
            }

            try:
                # --- ÉTAPE 1 : Création de la session (Checkout) ---
                url_checkout = f"{base_url}/create-checkout-session/"
                response_checkout = session.post(url_checkout, json=checkout_payload, headers=headers, timeout=15)

                # Gestion du 401 (API Key expirée/modifiée)
                if response_checkout.status_code == 401:
                    logger.warning("Clé d'API rejetée. Forçage du rafraîchissement du cache...")
                    config = cls._get_config(compte_id, force_refresh=True)
                    headers["Authorization"] = f"Api-Key {config['api_key']}"
                    response_checkout = session.post(url_checkout, json=checkout_payload, headers=headers, timeout=15)

                response_checkout.raise_for_status()
                checkout_data = response_checkout.json()

                session_token = checkout_data.get('session_token')
                paygate_reference = checkout_data.get('reference')

                # --- ÉTAPE 2 : Déclenchement de la Collecte ---
                url_collect = f"{base_url}/collect/"
                collect_payload = {
                    "phone_number": local_phone,
                    "session_token": session_token
                }

                public_headers = {"Content-Type": "application/json"}

                logger.info(f"[PayGate] Envoi du Push USSD collect pour le token {session_token}")
                response_collect = session.post(url_collect, json=collect_payload, headers=public_headers, timeout=15)
                response_collect.raise_for_status()

                return {
                    "paygate_reference": paygate_reference,
                    "session_token": session_token,
                    "collect_response": response_collect.json()
                }

            except requests.exceptions.RequestException as e:
                if e.response is not None:
                    logger.error(f"Détails de l'erreur renvoyée par la passerelle : {e.response.text}")

                # 🚀 Remplacement par logger.exception
                logger.exception(f"Erreur lors du traitement du flux de paiement pour la ref {external_reference}")

                # 🚀 Utilisation de ErrorCodes et dev_message
                raise CustomAPIException(
                    resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                    status_code=502,
                    dev_message=f"Erreur d'enchaînement sur la Gateway : {str(e)}"
                )

    @classmethod
    def verifier_statut_transaction(cls, compte_id, gateway_reference):
        """
        Vérifie le statut d'une transaction directement auprès de PayGate (Pull).
        Sert de filet de sécurité si le Webhook est perdu ou en retard.
        """
        if not gateway_reference:
            raise ValueError("Une gateway_reference est requise pour vérifier la transaction.")

        config = cls._get_config(compte_id)
        base_url = config['base_url']
        url = f"{base_url}/transaction/{gateway_reference}/"

        headers = {
            "Authorization": f"Api-Key {config['api_key']}",
            "Content-Type": "application/json"
        }

        try:
            logger.info(f"[PayGate] Vérification manuelle de la transaction {gateway_reference} (Compte {compte_id})")
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 401:
                logger.warning("Clé d'API rejetée lors de la vérification. Forçage du rafraîchissement du cache...")
                config = cls._get_config(compte_id, force_refresh=True)
                headers["Authorization"] = f"Api-Key {config['api_key']}"
                response = requests.get(url, headers=headers, timeout=10)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            if e.response is not None:
                logger.error(f"Détails Échec Vérification PayGate (Ref: {gateway_reference}) : {e.response.text}")

            # 🚀 Remplacement par logger.exception
            logger.exception(f"Erreur API Verify [{url}]")

            # 🚀 Utilisation de ErrorCodes et dev_message
            raise CustomAPIException(
                resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                status_code=502,
                dev_message=f"Erreur Gateway Verify [{url}]: {str(e)}"
            )