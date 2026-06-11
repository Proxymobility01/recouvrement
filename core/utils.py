import unicodedata
import json
import redis
from django.conf import settings


def remove_accents(texte):
    if not texte:
        return ""
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(texte))
        if unicodedata.category(c) != 'Mn'
    ).lower()


def format_phone_cm(phone):
    """
    Nettoie et force le formatage des numéros de téléphone au standard camerounais (237XXXXXXXXX).
    - Enlève les espaces, les signes '+' ou tirets.
    - Ajoute '237' si le numéro fait 9 chiffres.
    """
    if not phone:
        return ""

    # Enlever le +, les espaces et les tirets éventuels
    clean_phone = str(phone).replace("+", "").replace(" ", "").replace("-", "").strip()

    # Si le numéro est saisi sur 9 chiffres (ex: 690169694), on ajoute le code pays
    if len(clean_phone) == 9:
        return f"237{clean_phone}"

    return clean_phone




def notifier_utilisateur(compte_id: int, user_id: int, event_type: str, data: dict):
    """
    Envoie un événement SSE à un canal utilisateur strictement privé.
    Sécurise l'isolation Multi-Tenant (compte_id) et individuelle (user_id).
    """
    # Connexion synchrone standard pour l'expéditeur (Publisher)
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True
    )

    # Construction du canal hermétique
    canal_prive = f"notifications:{compte_id}:{user_id}"

    # Encapsulation propre du payload
    payload = json.dumps({
        "type": event_type,
        "data": data
    })

    # Publication immédiate (Fire and Forget)
    r.publish(canal_prive, payload)
    r.close()