"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os
import sys
from pathlib import Path

# Algunos hosts (por ejemplo Vercel) importan este archivo por ruta absoluta
# sin agregar la carpeta del proyecto a sys.path, así que "config" no se
# resuelve. Lo dejamos explícito para que funcione en cualquier entorno.
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from django.core.wsgi import get_wsgi_application  # noqa: E402

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()
