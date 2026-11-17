"""Configuración exclusiva de `collectstatic` durante el build.

No lee `.env` ni variables sensibles: compilar estáticos no debe depender de
secretos, y en Vercel el build corre antes de que exista la función.
"""
from .settings_base import *  # noqa: F403

try:
    import whitenoise  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
