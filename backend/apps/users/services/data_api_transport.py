"""Pools de Data API por request; los clientes y su estado siguen siendo privados."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from hashlib import sha256
from threading import get_ident

import httpx
from django.conf import settings
from django.views.decorators.debug import sensitive_variables


_REQUEST_POOL = ContextVar("planb_data_api_request_pool", default=None)
_OPERATION = ContextVar("planb_data_api_operation", default=None)


class _RequestPool:
    def __init__(self):
        self.owner = get_ident()
        self.closed = False
        self.transports = {}
        self.resources = ExitStack()

    @property
    def active(self):
        # Un contexto copiado a un worker no presta conexiones del request padre.
        return not self.closed and self.owner == get_ident() and _REQUEST_POOL.get() is self

    def transport(self, key, factory):
        if key not in self.transports:
            self.transports[key] = self.resources.enter_context(factory())
        return self.transports[key]

    def close(self):
        self.closed = True
        try:
            self.resources.close()
        finally:
            self.transports.clear()


@contextmanager
def request_connections():
    pool = _RequestPool()
    marker = _REQUEST_POOL.set(pool)
    try:
        yield
    finally:
        try:
            pool.close()
        finally:
            _REQUEST_POOL.reset(marker)


@contextmanager
@sensitive_variables()
def data_api_operation(api_key, access_token):
    pool = _REQUEST_POOL.get()
    binding = None
    if (pool is not None and pool.active and access_token
            and api_key == settings.SUPABASE_PUBLISHABLE_KEY):
        binding = (pool, access_token)
    marker = _OPERATION.set(binding)
    try:
        yield
    finally:
        _OPERATION.reset(marker)


def _route_key(options):
    limits = options["limits"]
    proxy = options.get("proxy")
    return (
        options["verify"], options["cert"], options["trust_env"],
        options["http1"], options["http2"],
        limits.max_connections, limits.max_keepalive_connections, limits.keepalive_expiry,
        None if proxy is None else (str(proxy.url), tuple(proxy.headers.raw), proxy.ssl_context),
    )


class _DataAPIRoute(httpx.BaseTransport):
    @sensitive_variables()
    def __init__(self, binding, factory, route_key):
        self.pool, self.access_token = binding
        # La clave del pool no conserva el JWT. Tokens diferentes nunca comparten pool.
        self.key = (sha256(self.access_token.encode()).digest(), route_key)
        self.base = httpx.URL(settings.SUPABASE_URL.rstrip("/") + "/rest/v1/")
        self.api_key = settings.SUPABASE_PUBLISHABLE_KEY
        self.factory = factory
        self.private = None

    @sensitive_variables()
    def handle_request(self, request):
        url = request.url
        compatible = (
            self.pool.active
            and (url.scheme, url.host, url.port) == (self.base.scheme, self.base.host, self.base.port)
            and url.path.startswith(self.base.path)
            and request.headers.get("apikey") == self.api_key
            and request.headers.get("Authorization") == f"Bearer {self.access_token}"
        )
        if compatible:
            transport = self.pool.transport(self.key, self.factory)
        else:
            # Auth, Storage y cualquier otro destino mantienen un transporte privado.
            if self.private is None:
                self.private = self.factory()
            transport = self.private
        return transport.handle_request(request)

    def close(self):
        if self.private is not None:
            self.private.close()
            self.private = None
        # El pool compartido pertenece al request, nunca a este cliente efímero.


class _DataAPIHTTPClient(httpx.Client):
    def __init__(self, binding, **kwargs):
        self._data_api_binding = binding
        super().__init__(**kwargs)

    # Hooks de HTTPX 0.28.1 (versión fijada). HTTPX sigue resolviendo proxies y
    # NO_PROXY; no se pasa transport= al constructor, que desactivaría esa lógica.
    def _init_transport(self, **kwargs):
        if kwargs.get("transport") is not None:
            return super()._init_transport(**kwargs)
        options = {key: value for key, value in kwargs.items() if key != "transport"}
        return _DataAPIRoute(self._data_api_binding,
                             lambda: httpx.HTTPTransport(**options), _route_key(options))

    def _init_proxy_transport(self, proxy, **kwargs):
        options = {**kwargs, "proxy": proxy}
        return _DataAPIRoute(self._data_api_binding,
                             lambda: httpx.HTTPTransport(**options), _route_key(options))


def http_client_class():
    binding = _OPERATION.get()
    if binding is not None and binding[0].active:
        return lambda **kwargs: _DataAPIHTTPClient(binding, **kwargs)
    return httpx.Client
