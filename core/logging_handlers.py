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

        # 🚀 L'ASTUCE POUR EXTRAIRE LA TRACE PROPREMENT
        if record.exc_info:
            # Si Django nous passe l'objet d'erreur directement
            tb = ''.join(traceback.format_exception(*record.exc_info))
        else:
            message_brut = record.getMessage()
            # Si l'erreur est enfouie dans le gros texte de Django
            if "Traceback (most recent call last):" in message_brut:
                try:
                    # 1. On coupe tout ce qui est avant la trace
                    partie_traceback = message_brut.split("Traceback (most recent call last):")[1]

                    # 2. On coupe tout ce qui est après la trace (les infos de requête)
                    if "Raised during:" in partie_traceback:
                        tb_propre = partie_traceback.split("Raised during:")[0].strip()
                    else:
                        tb_propre = partie_traceback.split("Request information:")[0].strip()

                    tb = f"Traceback (most recent call last):\n{tb_propre}"
                except Exception:
                    tb = message_brut
            else:
                tb = message_brut

        # 3. On formate l'e-mail final
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