from rest_framework import status
import logging
from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler
from rest_framework.exceptions import APIException, ValidationError

from .errors import ErrorCodes, ERROR_MESSAGES

logger = logging.getLogger(__name__)
django_request_logger = logging.getLogger('django.request')


class CustomAPIException(APIException):
    """
    Classe centralisée pour lever des erreurs métier.
    Génère automatiquement le payload final {message, dev_message}.
    """

    def __init__(self, resp_code, status_code=400, dev_message=None):
        self.resp_code = resp_code
        self.status_code = status_code
        self.dev_message = dev_message or "Aucun détail technique fourni."

        super().__init__(detail=self.dev_message)


def centralized_exception_handler(exc, context):
    """
    Intercepte toutes les erreurs DRF pour formater la réponse.
    """
    response = exception_handler(exc, context)

    # Valeurs par défaut (Erreur 500)
    dev_message = str(exc)
    user_message = ERROR_MESSAGES.get(ErrorCodes.SYSTEM_ERROR, "Erreur interne serveur.")

    if response is not None:
        # 1. Nos exceptions métier personnalisées
        if isinstance(exc, CustomAPIException):
            dev_message = exc.dev_message
            user_message = ERROR_MESSAGES.get(exc.resp_code, "Erreur inattendue.")

        # 2. Erreurs de validation de DRF (ex: format d'email invalide)
        elif isinstance(exc, ValidationError):
            dev_message = response.data  # Garde le format dict/list de DRF
            user_message = ERROR_MESSAGES.get(ErrorCodes.INVALID_PAYLOAD, "Données invalides.")

        # 3. Autres erreurs DRF (Auth, Permissions, etc.)
        else:
            if isinstance(response.data, dict):
                dev_message = response.data.get('detail', str(response.data))
            else:
                dev_message = str(response.data)

            if response.status_code == 401:
                user_message = ERROR_MESSAGES.get(ErrorCodes.UNAUTHORIZED, "Authentification requise.")
            elif response.status_code == 403:
                user_message = ERROR_MESSAGES.get(ErrorCodes.FORBIDDEN, "Action non autorisée.")
            elif response.status_code == 404:
                user_message = ERROR_MESSAGES.get(ErrorCodes.NOT_FOUND, "Ressource introuvable.")
            elif response.status_code == 400:
                user_message = ERROR_MESSAGES.get(ErrorCodes.BAD_REQUEST, "Requête invalide.")

    else:
        # 🚀 GESTION DES ERREURS 500 CRITIQUES INATTENDUES 🚀
        request = context.get('request')
        path_info = request.path if request else 'Inconnu'

        # 🚀 Remplacement par logger.exception pour capturer la trace complète du crash
        django_request_logger.exception(f"Internal Server Error: {path_info}")
        logger.exception("Erreur serveur inattendue lors du traitement de la requête")

        # Création de la réponse d'échec
        response = Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        dev_message = f"Erreur critique du serveur : {exc.__class__.__name__} - {str(exc)}"
        user_message = ERROR_MESSAGES.get(ErrorCodes.SYSTEM_ERROR, "Erreur interne serveur.")

    # Format de payload standard et strict
    response.data = {
        "message": user_message,
        "dev_message": dev_message
    }

    return response


def custom_404_handler(request, exception=None):
    """
    Capture les URLs introuvables au niveau de Django et renvoie notre format JSON.
    """
    dev_message = f"L'URL demandée n'existe pas : {request.path}"

    if not request.path.endswith('/'):
        dev_message += " (Indice : Avez-vous oublié le '/' à la fin de l'URL ?)"

    return JsonResponse({
        "message": ERROR_MESSAGES.get(ErrorCodes.NOT_FOUND, "Ressource introuvable."),
        "dev_message": dev_message
    }, status=404)


def custom_500_handler(request, exception=None):
    """
    Capture les crashs critiques (hors API) au niveau de Django.
    """
    # Utilisation de exception ici aussi si un crash survient hors du cycle DRF
    logger.exception("Erreur critique capturée par le handler 500 Django global")

    return JsonResponse({
        "message": ERROR_MESSAGES.get(ErrorCodes.SYSTEM_ERROR, "Erreur interne serveur."),
        "dev_message": "Erreur critique inattendue du serveur Django."
    }, status=500)