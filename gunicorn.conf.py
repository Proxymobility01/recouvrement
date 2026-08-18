import multiprocessing
import environ
env = environ.Env()
# ==========================================
# RÉSEAU & LIAISON
# ==========================================
# Le port 8000 est celui attendu par ton docker-compose
bind = "0.0.0.0:8000"

# ==========================================
# PERFORMANCES (Workers & Threads)
# ==========================================
# Formule standard de Gunicorn pour exploiter à fond le CPU
workers = min((2 * multiprocessing.cpu_count()) + 1, 1)

worker_class = "uvicorn.workers.UvicornWorker"

worker_connections = 1000

# ==========================================
# GESTION DE LA MÉMOIRE (Anti-fuites)
# ==========================================
# Gunicorn redémarre un worker automatiquement après 1000 requêtes
preload_app = True
max_requests = 1000
# Le jitter évite que tous les workers redémarrent exactement en même temps
max_requests_jitter = 100

# ==========================================
# TIMEOUTS & STABILITÉ
# ==========================================
# 120 secondes max par requête (utile si ton API fait des appels externes un peu lents)
timeout = 120
graceful_timeout = 30
keepalive = 75

# ==========================================
# LOGS & DÉBOGAGE
# ==========================================
# '-' envoie les logs directement dans la console Docker (visible avec 'docker logs')
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'
capture_output = True

# ==========================================
# SÉCURITÉ & NOMMAGE
# ==========================================
proc_name = env("APP_PROC_NAME", default="recouvrement_backend")


# Limites pour éviter les attaques DDoS par requêtes malformées
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190