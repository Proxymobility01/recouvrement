import asyncio
import logging
import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse, HttpResponse, HttpRequest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.request import Request

from core.authentication import KeycloakJWTAuthentication

logger = logging.getLogger(__name__)
User = get_user_model()


@sync_to_async
def get_user_from_keycloak_token(token_str):
    """
    Pont asynchrone/synchrone : Simule une requête DRF classique
    pour forcer la classe Keycloak à valider le token et retourner l'utilisateur.
    """
    try:
        django_request = HttpRequest()
        django_request.META['HTTP_AUTHORIZATION'] = f"Bearer {token_str}"

        drf_request = Request(django_request)

        auth_backend = KeycloakJWTAuthentication()
        auth_result = auth_backend.authenticate(drf_request)

        if auth_result:
            user, token = auth_result
            return user
        return None

    except Exception as e:
        logger.error(f"[SSE Auth] Échec de l'authentification Keycloak : {str(e)}")
        return None


async def event_stream(compte_id, user_id, r_client):
    """
    Générateur asynchrone qui maintient la connexion ouverte et écoute Redis.
    """
    pubsub = r_client.pubsub()
    canal_prive = f"notifications:{compte_id}:{user_id}"

    try:
        await pubsub.subscribe(canal_prive)
        # Ping de connexion initial
        yield "data: {\"type\": \"connected\"}\n\n"

        while True:
            # On écoute sans bloquer le thread grâce au timeout
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)

            if message:
                yield f"data: {message['data'].decode()}\n\n"
            else:
                # Heartbeat toutes les 30s pour empêcher Nginx de fermer la connexion
                yield ": heartbeat\n\n"

    except asyncio.CancelledError:
        logger.info(f"[SSE] Déconnexion propre de l'utilisateur {user_id} du canal {canal_prive}")
        try:
            await pubsub.unsubscribe(canal_prive)
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"[SSE Stream Error] Erreur critique dans le flux pour l'user {user_id}: {str(e)}")
        yield f"data: {{\"type\": \"error\", \"message\": \"Flux interrompu côté serveur.\"}}\n\n"
    finally:
        # Fermeture propre des sockets Redis lies au pubsub
        try:
            await pubsub.close()
        except Exception:
            pass
        try:
            await r_client.aclose()
        except Exception:
            pass


async def sse_notifications(request):
    """
    Point d'entrée ASGI pour la diffusion des événements en temps réel.
    """
    token_str = request.GET.get('token')
    if not token_str:
        return HttpResponse("Unauthorized: Missing Token", status=401)

    # Validation du token via le pont Keycloak
    user = await get_user_from_keycloak_token(token_str)

    if not user:
        return HttpResponse("Unauthorized: Invalid Keycloak Token", status=401)

    # 🛠️ FIX DE L'AVERTISSEMENT IDE : Extraction sécurisée du compte_id de l'utilisateur personnalisé
    compte_id = getattr(user, 'compte_id', None)
    if not compte_id:
        logger.error(f"[SSE] L'utilisateur {user.id} ne possède pas d'attribut 'compte_id'.")
        return HttpResponse("Forbidden: User profile incomplete", status=403)

    # 🛡️ GESTION DES EXCEPTIONS REDIS
    try:
        r_client = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD or None
        )
        # Un ping rapide pour valider que le serveur Redis est en ligne avant de lancer le stream
        await r_client.ping()

    except Exception as e:
        logger.error(f"[SSE Redis Connection Error] Impossible de joindre Redis : {str(e)}")
        return HttpResponse("Service Unavailable: Realtime engine offline", status=503)

    # Création et configuration du tunnel de réponse long
    response = StreamingHttpResponse(
        event_stream(compte_id, user.id, r_client),
        content_type='text/event-stream'
    )

    response['Cache-Control'] = 'no-cache, must-revalidate'
    response['X-Accel-Buffering'] = 'no'
    response['Connection'] = 'keep-alive'
    response['Content-Encoding'] = 'identity'

    return response