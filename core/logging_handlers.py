import logging
import traceback
from django.core.mail import send_mail
from django.conf import settings

class SimpleAdminEmailHandler(logging.Handler):
    def emit(self, record):
        try:
            request = getattr(record, 'request', None)
            user = str(getattr(request, 'user', 'Anonyme'))
            method = getattr(request, 'method', '?')
            path = getattr(request, 'path', '?')
            ip = getattr(request, 'META', {}).get('HTTP_X_REAL_IP', '?')
        except Exception:
            user, method, path, ip = 'Inconnu', '?', '?', '?'

        # 1. On récupère le titre de l'erreur
        message_brut = record.getMessage()

        # 2. 🚀 On extrait et on assemble la trace proprement
        if record.exc_info:
            # Maintenant que exc_info=True, Python va nous donner la vraie trace !
            tb_str = ''.join(traceback.format_exception(*record.exc_info))
            tb = f"{message_brut}\n\nTraceback (most recent call last):\n{tb_str}"
        else:
            # Sécurité au cas où l'erreur vient d'ailleurs
            if "Traceback (most recent call last):" in message_brut:
                try:
                    partie_traceback = message_brut.split("Traceback (most recent call last):")[1]
                    if "Raised during:" in partie_traceback:
                        tb_propre = partie_traceback.split("Raised during:")[0].strip()
                    else:
                        tb_propre = partie_traceback.split("Request information:")[0].strip()
                    tb = f"Traceback (most recent call last):\n{tb_propre}"
                except Exception:
                    tb = message_brut
            else:
                tb = message_brut

        # 3. L'en-tête propre
        subject = f"[ERREUR 500] {method} {path}"
        message = (
            f"🔴 Erreur serveur\n"
            f"{'─' * 40}\n"
            f"URL      : {path}\n"
            f"Méthode  : {method}\n"
            f"User     : {user}\n"
            f"IP       : {ip}\n"
            f"{'─' * 40}\n\n"
            f"{tb}"
        )

        # 4. L'envoi indépendant
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.SERVER_EMAIL,
                recipient_list=[a[1] for a in settings.ADMINS],
                fail_silently=True,
            )
        except Exception as e:
            print(f"Erreur d'envoi d'e-mail admin : {e}")