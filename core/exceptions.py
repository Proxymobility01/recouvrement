from rest_framework.views import exception_handler
from rest_framework.exceptions import APIException, ValidationError
from .errors import ErrorCodes, ERROR_MESSAGES


class CustomAPIException(APIException):
    """
    Classe pour lever des erreurs métier n'importe où.
    """
    status_code = 400

    def __init__(self, dev_msg, resp_code, status_code=400, usr_msg=None):
        self.dev_msg = dev_msg
        self.resp_code = resp_code
        self.status_code = status_code
        self.usr_msg = usr_msg or ERROR_MESSAGES.get(resp_code, "Une erreur est survenue.")
        super().__init__(detail=self.dev_msg)


def centralized_exception_handler(exc, context):
    """
    Intercepte toutes les erreurs DRF pour formater la réponse.
    """
    response = exception_handler(exc, context)

    # Valeurs par défaut (Erreur 500)
    dev_msg = str(exc)
    usr_msg = ERROR_MESSAGES[ErrorCodes.SYSTEM_ERROR]
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
            usr_msg = ERROR_MESSAGES[ErrorCodes.INVALID_PAYLOAD]

        # 3. Autres erreurs DRF (Auth, Permissions, etc.)
        else:
            dev_msg = str(response.data.get('detail', response.data))

            if response.status_code == 401:
                resp_code = ErrorCodes.AUTH_INVALID_TOKEN
                usr_msg = ERROR_MESSAGES[ErrorCodes.AUTH_INVALID_TOKEN]
            elif response.status_code == 403:
                resp_code = ErrorCodes.ACCESS_DENIED
                usr_msg = ERROR_MESSAGES[ErrorCodes.ACCESS_DENIED]
            elif response.status_code == 404:
                resp_code = ErrorCodes.ENDPOINT_NOT_FOUND
                usr_msg = ERROR_MESSAGES[ErrorCodes.ENDPOINT_NOT_FOUND]
            elif response.status_code == 400:
                resp_code = ErrorCodes.INVALID_PAYLOAD
                usr_msg = ERROR_MESSAGES[ErrorCodes.INVALID_PAYLOAD]

        # Remplacement du payload par notre standard
        response.data = {
            "devMsg": dev_msg,
            "usrMsg": usr_msg,
            "respCode": resp_code,
            "link": link
        }

    return response