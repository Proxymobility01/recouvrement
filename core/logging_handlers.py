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

        # Récupérer le traceback s'il existe
        if record.exc_info:
            tb = ''.join(traceback.format_exception(*record.exc_info))
        else:
            tb = record.getMessage()

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

        send_mail(
            subject=subject,
            message=message,
            from_email=settings.SERVER_EMAIL,
            recipient_list=[a[1] for a in settings.ADMINS],
            fail_silently=True,
        )