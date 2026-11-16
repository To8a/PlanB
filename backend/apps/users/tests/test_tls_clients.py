"""TLS real y SDK aislado: sin red, credenciales reales ni base de datos."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, chdir
import os
from pathlib import Path
import shutil
import ssl
from tempfile import TemporaryDirectory
from unittest.mock import patch

import certifi
from django.conf import settings
from django.test import SimpleTestCase
import httpx

from apps.users.services import clients


# Material ficticio y público de tests, usado sólo en handshakes en memoria.
_TEST_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIBnzCCAUSgAwIBAgIBATAKBggqhkjOPQQDAjAYMRYwFAYDVQQDDA1hdWRpdC5p
bnZhbGlkMCAXDTIwMDEwMTAwMDAwMFoYDzIwOTkwMTAxMDAwMDAwWjAYMRYwFAYD
VQQDDA1hdWRpdC5pbnZhbGlkMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaLeJ
+0H0Xm+msVoyjeClArme3peanhl0tT8VfsKSK6E4llV4LiwrshqDwjG+9cPmYP1Z
KvuhPjSV9a7j5OgLGaN9MHswGAYDVR0RBBEwD4INYXVkaXQuaW52YWxpZDAPBgNV
HRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNVHQ4EFgQUasmD0vWeqZCg
gG+z3hS+rkN5wrUwHwYDVR0jBBgwFoAUasmD0vWeqZCggG+z3hS+rkN5wrUwCgYI
KoZIzj0EAwIDSQAwRgIhALLh4aoO4feHeWO/mKtY0CgPWHvWVGCyrNqhIu5YCuPg
AiEA434zA/w+i9f3rEi7YGdCStCznxsd2shaqQnrRsVWqhs=
-----END CERTIFICATE-----
"""
_TEST_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg36zm54VMGpJRAo9k
H/EuC3WZCFTP8gbMy6VXhzWiY4KhRANCAARot4n7QfReb6axWjKN4KUCuZ7el5qe
GXS1PxV+wpIroTiWVXguLCuyGoPCMb71w+Zg/Vkq+6E+NJX1ruPk6AsZ
-----END PRIVATE KEY-----
"""


class TLSClientTests(SimpleTestCase):
    def setUp(self):
        clients._cached_tls_context.cache_clear()
        self.addCleanup(clients._cached_tls_context.cache_clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
                     "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                     "all_proxy", "no_proxy"):
            os.environ.pop(name, None)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.network = ExitStack()
        self.addCleanup(self.network.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            self.network.enter_context(patch(target, side_effect=AssertionError("Red deshabilitada")))

    def ca_file(self, name="ca.pem"):
        path = self.directory / name
        shutil.copyfile(certifi.where(), path)
        return path

    def handshake(self, hostname):
        certificate = self.directory / "server.pem"
        key = self.directory / "server-key.pem"
        certificate.write_text(_TEST_CERTIFICATE, encoding="ascii")
        key.write_text(_TEST_PRIVATE_KEY, encoding="ascii")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate, key)
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = clients._tls_context().wrap_bio(client_in, client_out, server_hostname=hostname)
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        client_done = server_done = False
        for _ in range(10):
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
            server_in.write(client_out.read())
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
            client_in.write(server_out.read())
            if client_done and server_done:
                return
        self.fail("El handshake TLS en memoria no terminó")

    def test_tls_handshake_rejects_certificate_outside_trusted_ca(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake("audit.invalid")

    def test_tls_handshake_accepts_explicit_ca_and_rejects_wrong_hostname(self):
        ca = self.directory / "trusted.pem"
        ca.write_text(_TEST_CERTIFICATE, encoding="ascii")
        os.environ["SSL_CERT_FILE"] = str(ca)
        self.handshake("audit.invalid")
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            self.handshake("wrong.invalid")
        self.assertIn("hostname", str(caught.exception).lower())

    def test_default_context_reuses_trust_with_certificate_and_hostname_verification(self):
        context = clients._tls_context()
        self.assertIs(context, clients._tls_context())
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        reference = httpx.create_ssl_context()
        self.assertEqual(context.get_ca_certs(binary_form=True), reference.get_ca_certs(binary_form=True))
        self.assertEqual(context.minimum_version, reference.minimum_version)
        self.assertEqual(context.verify_flags, reference.verify_flags)

    def test_ca_file_environment_is_respected_and_loaded_only_once(self):
        path = self.ca_file()
        os.environ["SSL_CERT_FILE"] = str(path)
        with patch("ssl.create_default_context", wraps=ssl.create_default_context) as build:
            first = clients._tls_context()
            self.assertIs(first, clients._tls_context())
        build.assert_called_once_with(cafile=str(path.resolve()))

    def test_ca_file_takes_precedence_over_ca_directory_like_httpx(self):
        os.environ["SSL_CERT_FILE"] = str(self.ca_file())
        os.environ["SSL_CERT_DIR"] = str(self.directory / "missing-directory")
        context = clients._tls_context()
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        self.assertIs(context, clients._tls_context())

    def test_switching_ca_file_rebuilds_context_and_bounds_cache(self):
        os.environ["SSL_CERT_FILE"] = str(self.ca_file("first.pem"))
        first = clients._tls_context()
        os.environ["SSL_CERT_FILE"] = str(self.ca_file("second.pem"))
        self.assertIsNot(first, clients._tls_context())
        self.assertEqual(clients._cached_tls_context.cache_info().currsize, 1)

    def test_relative_ca_path_is_resolved_again_after_working_directory_changes(self):
        first_path = self.ca_file()
        other_directory = self.directory / "other"
        other_directory.mkdir()
        shutil.copyfile(first_path, other_directory / first_path.name)
        os.environ["SSL_CERT_FILE"] = first_path.name
        with chdir(self.directory):
            first = clients._tls_context()
        with chdir(other_directory):
            self.assertIsNot(first, clients._tls_context())

    def test_updating_ca_file_invalidates_cached_context(self):
        path = self.ca_file()
        os.environ["SSL_CERT_FILE"] = str(path)
        first = clients._tls_context()
        with path.open("ab") as output:
            output.write(b"\n")
        self.assertIsNot(first, clients._tls_context())

    def test_invalid_ca_replacement_cannot_reuse_previously_valid_context(self):
        path = self.ca_file()
        os.environ["SSL_CERT_FILE"] = str(path)
        clients._tls_context()
        path.write_text("invalid CA contents", encoding="ascii")
        with self.assertRaises(ssl.SSLError):
            clients.http_client()

    def test_missing_ca_file_keeps_httpx_failure_instead_of_falling_back_to_default_trust(self):
        os.environ["SSL_CERT_FILE"] = str(self.directory / "missing.pem")
        with self.assertRaises(OSError):
            clients.http_client()

    def test_ca_directory_uses_httpx_loading_without_caching(self):
        os.environ["SSL_CERT_DIR"] = str(self.directory)
        with patch.object(httpx, "create_ssl_context", wraps=httpx.create_ssl_context) as build:
            first = clients._tls_context()
            second = clients._tls_context()
        self.assertIsNot(first, second)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(clients._cached_tls_context.cache_info().currsize, 0)
        self.assertEqual(first.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(first.check_hostname)

    def test_removing_ca_environment_restores_default_trust(self):
        os.environ["SSL_CERT_FILE"] = str(self.ca_file())
        custom = clients._tls_context()
        del os.environ["SSL_CERT_FILE"]
        default = clients._tls_context()
        self.assertIsNot(custom, default)
        self.assertIs(default, clients._tls_context())

    def test_concurrent_first_use_builds_only_one_context(self):
        with patch("ssl.create_default_context", wraps=ssl.create_default_context) as build:
            with ThreadPoolExecutor(max_workers=4) as workers:
                contexts = list(workers.map(lambda _: clients._tls_context(), range(8)))
        self.assertEqual(build.call_count, 1)
        self.assertTrue(all(context is contexts[0] for context in contexts))

    def test_http_clients_keep_separate_pools_cookies_headers_and_hooks(self):
        with clients.http_client() as first, clients.http_client() as second:
            self.assertIsNot(first, second)
            self.assertIsNot(first._transport, second._transport)
            self.assertIsNot(first._transport._pool, second._transport._pool)
            self.assertIs(first._transport._pool._ssl_context, second._transport._pool._ssl_context)
            first.cookies.set("audit", "first-user")
            first.headers["Authorization"] = "Bearer first-user"
            first.event_hooks["request"].append(lambda request: None)
            self.assertNotIn("audit", second.cookies)
            self.assertNotIn("Authorization", second.headers)
            self.assertFalse(second.event_hooks["request"])
            self.assertEqual(first.timeout, httpx.Timeout(clients.HTTP_TIMEOUT))
            self.assertFalse(first.follow_redirects)
        self.assertTrue(first.is_closed)
        self.assertTrue(second.is_closed)

    def test_environment_proxy_changes_are_applied_to_each_new_client(self):
        context = clients._tls_context()
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            with self.subTest(variable=variable):
                for port in (8101, 8102):
                    os.environ[variable] = f"http://proxy.invalid:{port}"
                    with clients.http_client() as client:
                        scheme = "http" if variable == "HTTP_PROXY" else "https"
                        transport = client._transport_for_url(httpx.URL(f"{scheme}://service.invalid"))
                        self.assertEqual(transport._pool._proxy_url.port, port)
                        self.assertIs(transport._pool._ssl_context, context)
                del os.environ[variable]
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8101"
        os.environ["NO_PROXY"] = "service.invalid"
        with clients.http_client() as client:
            self.assertIs(client._transport_for_url(httpx.URL("https://service.invalid")), client._transport)

    def test_real_sdk_keeps_user_and_admin_state_separate_while_sharing_only_tls_context(self):
        requests = []

        def handle(transport, request):
            requests.append(request)
            return httpx.Response(200, json=[], request=request)

        with patch.object(httpx.HTTPTransport, "handle_request", handle):
            with clients.public_client("first-user-jwt") as first, \
                 clients.public_client("second-user-jwt") as second, \
                 clients.administrative_client() as admin:
                sdk_clients = (first, second, admin)
                transports = [client.options.httpx_client for client in sdk_clients]
                contexts = [transport._transport._pool._ssl_context for transport in transports]
                self.assertEqual(len({id(transport) for transport in transports}), 3)
                self.assertTrue(all(context is contexts[0] for context in contexts))
                self.assertEqual(len({id(client.options.storage) for client in sdk_clients}), 3)
                self.assertEqual(len({id(client.options.headers) for client in sdk_clients}), 3)
                for client in sdk_clients:
                    self.assertFalse(client.options.persist_session)
                    self.assertFalse(client.options.auto_refresh_token)
                    self.assertIsNone(client.auth._in_memory_session)
                transports[0].cookies.set("audit", "first-user")
                first.table("profiles").select("id").execute()
                second.table("profiles").select("id").execute()
                admin.table("profiles").select("id").execute()
                first.table("profiles").select("id").execute()
        self.assertEqual([request.headers.get("authorization") for request in requests],
                         ["Bearer first-user-jwt", "Bearer second-user-jwt", None, "Bearer first-user-jwt"])
        self.assertEqual([request.headers["apikey"] for request in requests],
                         [settings.SUPABASE_PUBLISHABLE_KEY, settings.SUPABASE_PUBLISHABLE_KEY,
                          settings.SUPABASE_SECRET_KEY, settings.SUPABASE_PUBLISHABLE_KEY])
        self.assertNotIn("cookie", requests[1].headers)
        self.assertNotIn("cookie", requests[2].headers)
        self.assertTrue(all(transport.is_closed for transport in transports))

    def test_transport_closes_on_failure_without_discarding_trust_context(self):
        context = clients._tls_context()
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with clients.public_client("user-jwt") as client:
                transport = client.options.httpx_client
                raise RuntimeError("synthetic failure")
        self.assertTrue(transport.is_closed)
        self.assertIs(context, clients._tls_context())
