import traceback
from django.utils.log import AdminEmailHandler


class SimpleAdminEmailHandler(AdminEmailHandler):
    def emit(self, record):
        try:
            request = getattr(record, 'request', None)
            user = str(getattr(request, 'user', 'Anonyme'))
            method = getattr(request, 'method', '?')
            path = getattr(request, 'path', '?')
            ip = getattr(request, 'META', {}).get('HTTP_X_REAL_IP', '?')
        except Exception:
            user, method, path, ip = 'Inconnu', '?', '?', '?'

        if record.exc_info:
            tb = ''.join(traceback.format_exception(*record.exc_info))
        else:
            tb = record.getMessage()

        record.msg = (
            f"URL      : {path}\n"
            f"Méthode  : {method}\n"
            f"User     : {user}\n"
            f"IP       : {ip}\n"
            f"{'─' * 40}\n\n"
            f"{tb}"
        )
        record.args = None

        super().emit(record)