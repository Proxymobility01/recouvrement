import logging
import traceback
from django.core.mail import send_mail
from django.conf import settings

# 🚀 On hérite de logging.Handler, PAS de AdminEmailHandler !
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

        # 🚀 On extrait la trace
        if record.exc_info:
            tb = ''.join(traceback.format_exception(*record.exc_info))
        else:
            message_brut = record.getMessage()
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

        # 🚀 On construit le sujet et le message
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

        # 🚀 On utilise send_mail directement (On court-circuite le comportement par défaut de Django)
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.SERVER_EMAIL,
                recipient_list=[a[1] for a in settings.ADMINS],
                fail_silently=True,
            )
        except Exception as e:
            # En cas de problème de connexion SMTP, on laisse une trace dans la console
            print(f"Erreur d'envoi d'e-mail admin : {e}")