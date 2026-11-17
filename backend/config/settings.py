"""Configuración de ejecución. Los tests utilizan settings_test por separado."""
import os
from dotenv import load_dotenv
from .settings_base import *  # noqa: F403
from .postgresql_pool import configure_pool_experiment

load_dotenv(BASE_DIR.parent / ".env")
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.getenv("DJANGO_DEBUG", "False").strip().lower() in {"true", "1", "yes"}
UNO_SUPABASE_ENABLED = os.getenv("UNO_SUPABASE_ENABLED", "false").strip().lower() in {"true", "1", "yes"}
UNO_MULTIPLAYER_ENABLED = os.getenv("UNO_MULTIPLAYER_ENABLED", "false").strip().lower() in {"true", "1", "yes"}
ALLOWED_HOSTS = [host.strip() for host in os.getenv(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",") if host.strip()]
DJANGO_PUBLIC_URL = os.getenv("DJANGO_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
CSRF_TRUSTED_ORIGINS = [DJANGO_PUBLIC_URL]
# Detrás de un proxy HTTPS (Vercel, Render, Fly): sin esto Django no ve la
# petición como segura y SECURE_SSL_REDIRECT entra en bucle.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ["SUPABASE_PUBLISHABLE_KEY"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
GEOAPIFY_API_KEY = os.getenv("GEOAPIFY_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or GEMINI_MODEL
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "CONN_MAX_AGE": 0,
    "NAME": os.environ["DB_NAME"], "USER": os.environ["DB_USER"],
    "PASSWORD": os.environ["DB_PASSWORD"], "HOST": os.environ["DB_HOST"],
    "PORT": os.getenv("DB_PORT", "5432"),
    "OPTIONS": {
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
        "options": "-c search_path=django_internal",
    },
}}
DATABASES["default"] = configure_pool_experiment(
    DATABASES["default"],
    enabled=os.getenv("PLANB_PG_POOL_EXPERIMENT", "").strip().lower() in {"true", "1", "yes"},
    local=DEBUG and not os.getenv("VERCEL"),
)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG

# Los estáticos los sirve el propio proceso WSGI cuando no hay nginx delante.
# `collectstatic` debe ejecutarse antes de arrancar (ver docs/deploy/vercel.md).
# Si el entorno local todavía no corrió `pip install -r requirements.txt`, la app
# sigue arrancando y Django sirve los estáticos como siempre.
try:
    import whitenoise  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    MIDDLEWARE = ["whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE]
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
