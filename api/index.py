"""Punto de entrada serverless de Vercel.

Vercel detecta este archivo como función Python y publica la variable ``app``
como aplicación WSGI. Django vive en ``backend/``, así que lo agregamos al
``sys.path`` antes de importar los settings.
"""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
