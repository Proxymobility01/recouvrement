from .base import *
from corsheaders.defaults import default_headers

# --- DEBUG ---
DEBUG = True

ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]
)

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:4200", "http://127.0.0.1:4200"])


CORS_ALLOW_HEADERS = list(default_headers) + [
    "ngrok-skip-browser-warning",
]


CSRF_TRUSTED_ORIGINS = [
    "https://obliviously-exoskeletal-reggie.ngrok-free.dev",
    "https://sirena-pillowy-jennifer.ngrok-free.dev",
]