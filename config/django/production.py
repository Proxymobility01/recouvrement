from .base import *

DEBUG = env.bool('DEBUG',default=False)

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=[])

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

