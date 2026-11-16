"""Clientes efímeros con estado y transportes privados por operación."""

from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path
import ssl
from threading import Lock

import certifi
import httpx
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from supabase import Client
from supabase.lib.client_options import SyncClientOptions

HTTP_TIMEOUT = 10.0
_TLS_CONTEXT_LOCK = Lock()


@lru_cache(maxsize=1)
def _cached_tls_context(cafile, fingerprint):
    # Sólo confianza TLS: nunca contiene JWT, cookies ni estado de Supabase.
    # La huella invalida la entrada si se reemplaza o actualiza el archivo CA.
    return ssl.create_default_context(cafile=cafile)


def _tls_context():
    # Misma precedencia de confianza que HTTPX 0.28 con trust_env=True.
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile:
        if os.environ.get("SSL_CERT_DIR"):
            # OpenSSL carga un capath de forma diferida. Sus archivos internos
            # pueden cambiar sin cambiar el directorio: conservar la carga normal.
            return httpx.create_ssl_context()
        cafile = certifi.where()
    try:
        path = Path(cafile).resolve()
        info = path.stat()
    except OSError:
        # Mantener los errores de configuración de confianza del transporte.
        return httpx.create_ssl_context()
    fingerprint = (info.st_dev, info.st_ino, info.st_size,
                   info.st_mtime_ns, info.st_ctime_ns)
    # Evitar varias cargas simultáneas durante la primera solicitud del proceso.
    with _TLS_CONTEXT_LOCK:
        return _cached_tls_context(str(path), fingerprint)


def http_client():
    # El contexto propietario siempre cierra este transporte. No hay pool global.
    return httpx.Client(verify=_tls_context(), timeout=HTTP_TIMEOUT, follow_redirects=False)


@sensitive_variables()
def _remove_api_key_bearer(request):
    # SDK 2.31 agrega la API key como Bearer al construir el cliente y al salir.
    # Corregir en el transporte cubre ambos casos sin alterar los JWT de usuario.
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and (
        token == request.headers.get("apikey")
        or token.startswith(("sb_publishable_", "sb_secret_"))
    ):
        request.headers.pop("Authorization", None)


@contextmanager
@sensitive_variables()
def _client(api_key, access_token=None):
    with http_client() as transport:
        transport.event_hooks["request"].append(_remove_api_key_bearer)
        options = SyncClientOptions(
            persist_session=False,
            auto_refresh_token=False,
            httpx_client=transport,
            headers={"Authorization": f"Bearer {access_token}"} if access_token else {},
        )
        # create_client() consulta get_session(); este constructor no restaura nada.
        client = Client(settings.SUPABASE_URL, api_key, options=options)
        yield client


@contextmanager
@sensitive_variables()
def public_client(access_token=None):
    with _client(settings.SUPABASE_PUBLISHABLE_KEY, access_token) as client:
        yield client


@contextmanager
@sensitive_variables()
def administrative_client():
    # Nunca recibe un JWT de usuario ni ejecuta login/refresh.
    with _client(settings.SUPABASE_SECRET_KEY) as client:
        yield client
