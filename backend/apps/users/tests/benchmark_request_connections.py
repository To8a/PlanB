"""Laboratorio opt-in: TCP/TLS reales sólo en loopback, SDK y datos ficticios.

Ejecutar desde backend: ../.venv/Scripts/python.exe -B -m
apps.users.tests.benchmark_request_connections
No forma parte de la suite offline ni se conecta al proyecto Supabase.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
from statistics import median
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from time import perf_counter
from unittest.mock import patch


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        raw, address = super().get_request()
        self.metrics["tcp"] += 1
        try:
            connection = self.tls.wrap_socket(raw, server_side=True)
        except BaseException:
            raw.close()
            raise
        self.metrics["tls"] += 1
        return connection, address


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        from .test_sdk_contracts import user_payload
        with self.server.metrics_lock:
            self.server.metrics["http"] += 1
        payload = {"user": user_payload()} if self.path.startswith("/auth/v1/user") else []
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def local_https():
    from .test_tls_clients import _TEST_CERTIFICATE, _TEST_PRIVATE_KEY
    with TemporaryDirectory(prefix="planb-http-lab-") as directory:
        cert = Path(directory) / "fixture.pem"
        key = Path(directory) / "fixture-key.pem"
        cert.write_text(_TEST_CERTIFICATE, encoding="ascii")
        key.write_text(_TEST_PRIVATE_KEY, encoding="ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        with FixtureServer(("127.0.0.1", 0), FixtureHandler) as server:
            server.tls = context
            server.metrics_lock = Lock()
            server.metrics = {"tcp": 0, "tls": 0, "http": 0}
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            original_resolve = socket.getaddrinfo
            original_connect = socket.socket.connect
            port = server.server_port

            def resolve(host, target_port, *args, **kwargs):
                if host not in ("audit.invalid", b"audit.invalid") or target_port != port:
                    raise AssertionError("El laboratorio sólo permite su servidor local")
                return original_resolve("127.0.0.1", port, *args, **kwargs)

            def connect(sock, address):
                if address != ("127.0.0.1", port):
                    raise AssertionError("Conexión fuera del laboratorio local")
                return original_connect(sock, address)

            try:
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert), "NO_PROXY": "*"}), \
                     patch("socket.getaddrinfo", resolve), patch("socket.socket.connect", connect), \
                     patch("socket.socket.connect_ex", side_effect=AssertionError("connect_ex no autorizado")):
                    yield server, f"https://audit.invalid:{port}"
            finally:
                server.shutdown()
                thread.join(timeout=5)


def main():
    # Nunca cargar settings de ejecución ni .env, incluso si el shell los heredó.
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_test"
    import django
    django.setup()
    from django.http import HttpResponse
    from django.test import override_settings
    from apps.users.middleware import DataAPIConnectionsMiddleware
    from apps.users.services import auth_service, clients

    def fixture_view(request):
        auth_service.get_user("fixture-user-jwt")
        for table in ("profiles", "groups", "notifications"):
            with clients.public_client("fixture-user-jwt") as client:
                client.table(table).select("id").execute()
        with clients.administrative_client() as client:
            client.table("profiles").select("id").execute()
        return HttpResponse("ok")

    results = []
    original_http_client = clients.http_client
    try:
        with local_https() as (server, url), override_settings(SUPABASE_URL=url):
            clients._cached_tls_context.cache_clear()
            clients._tls_context()  # Comparar conexiones con confianza ya inicializada.
            for repetition in range(1, 6):
                # Alternar orden para reducir sesgo por inicialización/orden.
                modes = (False, True) if repetition % 2 else (True, False)
                for reuse in modes:
                    server.metrics = {"tcp": 0, "tls": 0, "http": 0}
                    client_count = 0
                    starts, phases = {}, []

                    def trace(event, info):
                        operation, _, kind = event.rpartition(".")
                        if operation in ("connection.connect_tcp", "connection.start_tls"):
                            if kind == "started":
                                starts[operation] = perf_counter()
                            elif kind == "complete":
                                phases.append((perf_counter() - starts.pop(operation)) * 1000)

                    def instrument_client():
                        nonlocal client_count
                        client_count += 1
                        client = original_http_client()
                        client.event_hooks["request"].append(lambda request: request.extensions.update(trace=trace))
                        return client

                    view = DataAPIConnectionsMiddleware(fixture_view) if reuse else fixture_view
                    with patch.object(clients, "http_client", side_effect=instrument_client):
                        start = perf_counter()
                        response = view(None)
                        elapsed = (perf_counter() - start) * 1000
                        response.close()
                    row = {"mode": "request_pool" if reuse else "baseline", "sample": repetition,
                           "clients": client_count, **server.metrics,
                           "connect_ms": round(sum(phases), 3), "view_ms": round(elapsed, 3)}
                    assert row["clients"] == 5 and row["http"] == 5
                    assert row["tcp"] == row["tls"] == (3 if reuse else 5)
                    results.append(row)
            summary = []
            for mode in ("baseline", "request_pool"):
                samples = [row for row in results if row["mode"] == mode]
                summary.append({"mode": mode, "samples": len(samples),
                                **{name: median(row[name] for row in samples)
                                   for name in ("clients", "tcp", "tls", "http", "connect_ms", "view_ms")}})
            print(json.dumps({"experiment": "loopback_https_fixtures", "results": results,
                              "medians": summary}, indent=2))
    finally:
        clients._cached_tls_context.cache_clear()


if __name__ == "__main__":
    main()
