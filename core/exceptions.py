from rest_framework import status
import logging
from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler
from rest_framework.exceptions import APIException, ValidationError
from .errors import ErrorCodes, ERROR_MESSAGES_USR, ERROR_MESSAGES_DEV

logger = logging.getLogger(__name__)

django_request_logger = logging.getLogger('django.request')
class CustomAPIException(APIException):
    """
    Classe centralisée pour lever des erreurs métier.
    Génère automatiquement les messages selon le code fourni.
    """

    def __init__(self, resp_code, status_code=400, context=None):
        self.resp_code = resp_code
        self.status_code = status_code

        # Récupération automatique depuis les dictionnaires
        self.usr_msg = ERROR_MESSAGES_USR.get(resp_code, "Une erreur inattendue est survenue.")
        base_dev_msg = ERROR_MESSAGES_DEV.get(resp_code, "Erreur inconnue dans le dictionnaire DEV.")

        # Ajout du contexte si fourni
        self.dev_msg = f"{base_dev_msg} [Context: {context}]" if context else base_dev_msg

        super().__init__(detail=self.dev_msg)


def centralized_exception_handler(exc, context):
    """
    Intercepte toutes les erreurs DRF pour formater la réponse.
    """
    response = exception_handler(exc, context)

    # Valeurs par défaut (Erreur 500)
    dev_msg = str(exc)
    usr_msg = ERROR_MESSAGES_USR[ErrorCodes.SYSTEM_ERROR]
    resp_code = ErrorCodes.SYSTEM_ERROR
    link = "http://support.com/"  # Ton lien de documentation API

    if response is not None:
        # 1. Nos exceptions métier personnalisées
        if isinstance(exc, CustomAPIException):
            dev_msg = exc.dev_msg
            usr_msg = exc.usr_msg
            resp_code = exc.resp_code

        # 2. Erreurs de validation de DRF (ex: format d'email invalide)
        elif isinstance(exc, ValidationError):
            dev_msg = response.data  # Garde le format dict de DRF {"champ": ["erreur"]}
            resp_code = ErrorCodes.INVALID_PAYLOAD
            usr_msg = ERROR_MESSAGES_USR[ErrorCodes.INVALID_PAYLOAD]

        # 3. Autres erreurs DRF (Auth, Permissions, etc.)
        else:
            dev_msg = str(response.data.get('detail', response.data))

            if response.status_code == 401:
                resp_code = ErrorCodes.AUTH_INVALID_TOKEN
                usr_msg = ERROR_MESSAGES_USR[ErrorCodes.AUTH_INVALID_TOKEN]
            elif response.status_code == 403:
                resp_code = ErrorCodes.ACCESS_DENIED
                usr_msg = ERROR_MESSAGES_USR[ErrorCodes.ACCESS_DENIED]
            elif response.status_code == 404:
                resp_code = ErrorCodes.ENDPOINT_NOT_FOUND
                usr_msg = ERROR_MESSAGES_USR[ErrorCodes.ENDPOINT_NOT_FOUND]
            elif response.status_code == 400:
                resp_code = ErrorCodes.INVALID_PAYLOAD
                usr_msg = ERROR_MESSAGES_USR[ErrorCodes.INVALID_PAYLOAD]

    else:
        # 🚀 C'EST ICI QUE LA MAGIE OPÈRE POUR LES ERREURS 500 INATTENDUES 🚀

        request = context.get('request')

        django_request_logger.error(
            f"Internal Server Error: {request.path if request else 'Inconnu'}",
            exc_info=exc,
            extra={'status_code': 500, 'request': request._request if hasattr(request, '_request') else request}
        )

        # 1. On affiche la trace complète dans le terminal pour le débogage
        logger.error(f"Erreur serveur inattendue : {str(exc)}", exc_info=True)

        # 2. On crée une réponse DRF de toutes pièces car DRF a abandonné
        response = Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 3. On sécurise le message technique pour ne pas fuiter de données sensibles
        dev_msg = f"Erreur critique du serveur : {exc.__class__.__name__} - {str(exc)}"
        # (usr_msg et resp_code sont déjà configurés sur SYSTEM_ERROR par défaut)

    # Remplacement du payload par notre standard pour TOUTES les réponses
    response.data = {
        "devMsg": dev_msg,
        "usrMsg": usr_msg,
        "respCode": resp_code,
        "link": link
    }

    return response


def custom_404_handler(request, exception=None):
    """
    Capture les URLs introuvables au niveau de Django et renvoie notre format JSON.
    """
    dev_msg = f"L'URL demandée n'existe pas : {request.path}"

    # 🚀 Petite aide hyper pratique pour ton développeur Front-End
    if not request.path.endswith('/'):
        dev_msg += " (Indice : Avez-vous oublié le '/' à la fin de l'URL ?)"

    return JsonResponse({
        "devMsg": dev_msg,
        "usrMsg": ERROR_MESSAGES_USR[ErrorCodes.ENDPOINT_NOT_FOUND],
        "respCode": ErrorCodes.ENDPOINT_NOT_FOUND,
        "link": "http://support.com/"
    }, status=404)


def custom_500_handler(request, exception=None):
    """
    Capture les crashs critiques (hors API) au niveau de Django.
    """
    return JsonResponse({
        "devMsg": "Erreur critique inattendue du serveur Django.",
        "usrMsg": ERROR_MESSAGES_USR[ErrorCodes.SYSTEM_ERROR],
        "respCode": ErrorCodes.SYSTEM_ERROR,
        "link": "http://support.com/"
    }, status=500)