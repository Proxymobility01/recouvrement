import logging

import requests
from django.core.cache import cache

from accounts.models import ConfigPaiement
from core.errors import ErrorCodes
from core.exceptions import CustomAPIException
from core.utils import format_phone_cm

logger = logging.getLogger(__name__)


class PaymentService:
    """
    Service Multi-Tenant pour les paiements Mobile Money.

    RÈGLES MÉTIER
    -------------
    1. Checkout échoue  → rien n'existe nulle part → ECHEC certain.
    2. Checkout réussit → session créée côté PayGate, référence persistée immédiatement.
    3. Collect échoue   → session PayGate existe mais le push USSD n'est PAS parti
                          → on remonte l'erreur brute de PayGate à l'utilisateur,
                            le statut local reste EN_ATTENTE (peut réessayer).
    4. Collect réussit  → push USSD déclenché → EN_ATTENTE + polling.
    """

    # ------------------------------------------------------------------ #
    # CONFIG (cache Redis 24h)
    # ------------------------------------------------------------------ #
    @classmethod
    def _get_config(cls, compte_id, force_refresh=False):
        cache_key = f"credentials_{compte_id}"

        if force_refresh:
            cache.delete(cache_key)
            logger.info(f"🔄 Cache de configuration invalidé pour le compte {compte_id}.")
        else:
            cached_config = cache.get(cache_key)
            if cached_config:
                return cached_config

        config = ConfigPaiement.objects.filter(compte_id=compte_id).first()
        if not config:
            logger.error(f"[PayGate] compte_id {compte_id} sans configuration de paiement.")
            raise CustomAPIException(
                resp_code=ErrorCodes.SYSTEM_ERROR,
                status_code=400,
                dev_message=f"Le partenaire (compte_id: {compte_id}) n'a pas configuré ses identifiants."
            )

        config_data = {
            "api_key":     config.api_key,
            "base_url":    config.base_url.rstrip('/'),
            "success_url": config.success_url,
        }
        cache.set(cache_key, config_data, 86400)
        return config_data

    # ------------------------------------------------------------------ #
    # INITIATION : checkout puis collect
    # ------------------------------------------------------------------ #
    @classmethod
    def traiter_paiement_complet(cls, compte_id, montant, external_reference, phone_number):
        """
        Enchaîne checkout puis collect dans une session HTTP unique (Keep-Alive).

        Retour possible :
          {
              "paygate_reference": str,   # toujours présent si le checkout a réussi
              "session_token":     str,
              "collect_ok":        bool,
              "collect_error":     str | None,  # message brut PayGate si collect KO
          }

        Lève CustomAPIException si le CHECKOUT échoue
        (rien n'a été créé côté PayGate → ECHEC certain côté appelant).
        """
        config      = cls._get_config(compte_id)
        base_url    = config['base_url']
        local_phone = format_phone_cm(phone_number)

        with requests.Session() as http:

            # ============================================================
            # ÉTAPE 1 : CHECKOUT
            # Échec → lève une exception (rien n'existe).
            # ============================================================
            url_checkout = f"{base_url}/create-checkout-session/"
            checkout_payload = {
                "amount":             int(montant),
                "phone_number":       local_phone,
                "external_reference": external_reference,
                "currency":           "XAF",
                "success_url":        config['success_url'],
                "cancel_url":         "",
            }

            try:
                headers = cls._auth_headers(config['api_key'])
                response_checkout = http.post(
                    url_checkout, json=checkout_payload, headers=headers, timeout=15
                )

                # Clé expirée : on rafraîchit le cache et on retente une fois.
                if response_checkout.status_code == 401:
                    logger.warning("[PayGate] Clé d'API rejetée au checkout. Rafraîchissement...")
                    config  = cls._get_config(compte_id, force_refresh=True)
                    headers = cls._auth_headers(config['api_key'])
                    response_checkout = http.post(
                        url_checkout, json=checkout_payload, headers=headers, timeout=15
                    )

                response_checkout.raise_for_status()

            except requests.exceptions.RequestException as e:
                code_http = 504
                details   = "Le serveur n'a pas répondu (Timeout/Coupure réseau)."
                if e.response is not None:
                    code_http = e.response.status_code
                    details   = e.response.text
                    logger.error(f"[PayGate] Détail erreur checkout : {details}")
                logger.exception(f"[PayGate] Échec du checkout pour la ref {external_reference}")
                raise CustomAPIException(
                    resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                    status_code=code_http,
                    dev_message=f"Checkout ({code_http}) : {details}"
                )

            try:
                checkout_data = response_checkout.json()
            except ValueError:
                raise CustomAPIException(
                    resp_code=ErrorCodes.SYSTEM_ERROR,
                    status_code=502,
                    dev_message="PayGate a répondu au checkout avec un format invalide (Non-JSON)."
                )

            session_token     = checkout_data.get('session_token')
            paygate_reference = checkout_data.get('reference')

            if not paygate_reference:
                raise CustomAPIException(
                    resp_code=ErrorCodes.SYSTEM_ERROR,
                    status_code=502,
                    dev_message="PayGate a validé le checkout sans renvoyer de référence."
                )

            # ============================================================
            # ÉTAPE 2 : COLLECT
            # La session PayGate existe. On tente le push USSD.
            # On ne lève JAMAIS d'exception ici : on remonte collect_ok + collect_error.
            # ============================================================
            url_collect = f"{base_url}/collect/"
            collect_payload = {
                "phone_number":  local_phone,
                "session_token": session_token,
            }
            public_headers = {"Content-Type": "application/json"}

            collect_ok    = False
            collect_error = None

            try:
                logger.info(
                    f"[PayGate] Envoi du push USSD (collect) pour le token {session_token}"
                )
                response_collect = http.post(
                    url_collect, json=collect_payload,
                    headers=public_headers, timeout=15
                )
                response_collect.raise_for_status()
                response_collect.json()   # on valide que le corps est lisible
                collect_ok = True

            except requests.exceptions.RequestException as e:
                # PayGate a renvoyé une réponse structurée (4xx/5xx) ou coupure réseau.
                if e.response is not None:
                    try:
                        body = e.response.json()
                        # On cherche le message le plus lisible dans la réponse PayGate.
                        collect_error = (
                            body.get('message')
                            or body.get('detail')
                            or body.get('error')
                            or str(body)
                        )
                    except ValueError:
                        collect_error = e.response.text or f"HTTP {e.response.status_code}"
                else:
                    collect_error = "La passerelle n'a pas répondu (timeout ou coupure réseau)."

                logger.warning(
                    f"[PayGate] Collect KO pour {paygate_reference}. "
                    f"Erreur : {collect_error}"
                )

            except ValueError:
                collect_error = "Réponse illisible reçue de la passerelle après le collect."
                logger.warning(
                    f"[PayGate] Collect réponse non-JSON pour {paygate_reference}."
                )

            return {
                "paygate_reference": paygate_reference,
                "session_token":     session_token,
                "collect_ok":        collect_ok,
                "collect_error":     collect_error,
            }

    # ------------------------------------------------------------------ #
    # VÉRIFICATION (pull) — source de vérité pour le polling
    # ------------------------------------------------------------------ #
    @classmethod
    def verifier_statut_transaction(cls, compte_id, gateway_reference):
        """
        Interroge PayGate sur l'état réel d'une transaction.
        Filet de sécurité si le webhook est perdu ou en retard.
        """
        if not gateway_reference:
            # ValueError intentionnelle : le polling doit la distinguer
            # d'une vraie panne réseau pour ne pas boucler inutilement.
            raise ValueError("Une gateway_reference est requise pour vérifier la transaction.")

        config = cls._get_config(compte_id)
        url    = f"{config['base_url']}/transaction/{gateway_reference}/"

        try:
            logger.info(
                f"[PayGate] Vérification de la transaction "
                f"{gateway_reference} (Compte {compte_id})"
            )
            headers  = cls._auth_headers(config['api_key'])
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 401:
                logger.warning("[PayGate] Clé rejetée à la vérification. Rafraîchissement...")
                config   = cls._get_config(compte_id, force_refresh=True)
                headers  = cls._auth_headers(config['api_key'])
                response = requests.get(url, headers=headers, timeout=10)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            code_http = 504
            details   = "Impossible de joindre la passerelle pour vérification."
            if e.response is not None:
                code_http = e.response.status_code
                details   = e.response.text
                logger.error(
                    f"[PayGate] Détail échec vérification "
                    f"(Ref: {gateway_reference}) : {details}"
                )
            logger.exception(f"[PayGate] Erreur API Verify [{url}]")
            raise CustomAPIException(
                resp_code=ErrorCodes.MOBILE_MONEY_FAILED,
                status_code=code_http,
                dev_message=f"Vérification ({code_http}) : {details}"
            )

    # ------------------------------------------------------------------ #
    # Helper
    # ------------------------------------------------------------------ #
    @staticmethod
    def _auth_headers(api_key):
        return {
            "Authorization": f"Api-Key {api_key}",
            "Content-Type":  "application/json",
        }