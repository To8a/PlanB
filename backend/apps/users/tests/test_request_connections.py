"""SDK/HTTPX reales, con I/O sustituido: sin datos ni conexiones externas."""
import asyncio
import _socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
import os
import ssl
import socket
from threading import Barrier
from unittest.mock import patch

from django.conf import settings
from django.http import StreamingHttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.test import AsyncClient, SimpleTestCase, override_settings
from django.urls import path
import httpx

from apps.users.middleware import DataAPIConnectionsMiddleware
from apps.groups.services import group_service
from apps.groups.services.exceptions import GroupNotFound
from apps.users.services import auth_service, clients
from apps.users.services.data_api_transport import _REQUEST_POOL, request_connections
from .test_sdk_contracts import session_payload, user_payload


def read_data(token="fixture-user"):
    with clients.public_client(token) as client:
        client.table("groups").select("id").execute()
        return client


def synthetic_view(request):
    read_data()
    read_data()
    return TemplateResponse(request, engines["django"].from_string("{{ value }}"), {"value": "ok"})


def failing_template(request):
    read_data()

    class BrokenTemplate:
        def render(self, context, request):
            raise RuntimeError("synthetic render failure")

    return TemplateResponse(request, BrokenTemplate(), {})


def streaming_view(request):
    def content():
        read_data()
        read_data()
        yield b"ok"
    return StreamingHttpResponse(content())


urlpatterns = [path("synthetic/", synthetic_view), path("broken/", failing_template),
               path("stream/", streaming_view)]


class RequestConnectionsTests(SimpleTestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY",
                     "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            os.environ.pop(name, None)
        self.transports, self.requests, self.closed = [], [], []
        self.reply = None
        original_init, original_close = httpx.HTTPTransport.__init__, httpx.HTTPTransport.close
        original_exit = httpx.HTTPTransport.__exit__

        def initialize(transport, *args, **kwargs):
            original_init(transport, *args, **kwargs)
            self.transports.append(transport)

        def close(transport):
            self.closed.append(transport)
            original_close(transport)

        def exit_transport(transport, *args):
            # HTTPX 0.28.1 cierra el pool directamente desde __exit__, sin close().
            self.closed.append(transport)
            return original_exit(transport, *args)

        def handle(transport, request):
            self.requests.append((transport, request))
            if self.reply is not None:
                return self.reply(request)
            if request.url.path == "/auth/v1/user":
                payload = {"user": user_payload()}
            elif request.url.path == "/auth/v1/token":
                payload = session_payload()
            else:
                payload = []
            return httpx.Response(200, json=payload, request=request)

        for name, replacement in (("__init__", initialize), ("close", close),
                                  ("__exit__", exit_transport), ("handle_request", handle)):
            patcher = patch.object(httpx.HTTPTransport, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.assertIsNone(_REQUEST_POOL.get())
        self.assertCountEqual(self.closed, self.transports)

    def test_clients_keep_private_state_while_data_connections_remain_open_until_request_end(self):
        with request_connections():
            first = read_data()
            transport = self.transports[0]
            self.assertTrue(first.options.httpx_client.is_closed)
            self.assertNotIn(transport, self.closed)
            second = read_data()
            self.assertIsNot(first, second)
            self.assertIsNot(first.options.httpx_client, second.options.httpx_client)
            self.assertIsNot(first.options.storage, second.options.storage)
            self.assertIsNot(first.options.headers, second.options.headers)
            for client in (first, second):
                self.assertFalse(client.options.persist_session)
                self.assertFalse(client.options.auto_refresh_token)
                self.assertIsNone(client.auth._in_memory_session)
            self.assertEqual(len(self.transports), 1)
        self.assertIn(transport, self.closed)

    def test_different_tokens_in_same_scope_never_share_transport(self):
        with request_connections():
            for token in ("user-a", "user-b", "user-a"):
                read_data(token)
        used = [transport for transport, _ in self.requests]
        self.assertIs(used[0], used[2])
        self.assertIsNot(used[0], used[1])
        self.assertEqual([r.headers["authorization"] for _, r in self.requests],
                         ["Bearer user-a", "Bearer user-b", "Bearer user-a"])

    def test_cookies_headers_and_hooks_are_private_between_operations(self):
        self.reply = lambda request: httpx.Response(200, json=[], headers={"set-cookie": "fixture=private; Path=/"})
        with request_connections():
            with clients.public_client("user-a") as first:
                first.table("groups").select("id").execute()
                self.assertEqual(first.options.httpx_client.cookies.get("fixture"), "private")
                first.options.httpx_client.headers["X-Private"] = "first"
                first.options.httpx_client.event_hooks["request"].append(lambda r: r.headers.update({"X-Hook": "first"}))
            with clients.public_client("user-a") as second:
                self.assertFalse(second.options.httpx_client.cookies)
                second.table("groups").select("id").execute()
        self.assertEqual(len(self.transports), 1)
        self.assertNotIn("cookie", self.requests[1][1].headers)
        self.assertNotIn("x-private", self.requests[1][1].headers)
        self.assertNotIn("x-hook", self.requests[1][1].headers)

    def test_administrative_and_anonymous_clients_are_not_pooled(self):
        with request_connections():
            read_data()
            with clients.administrative_client() as admin:
                admin.table("profiles").select("id").execute()
            self.assertIn(self.requests[-1][0], self.closed)
            with clients.public_client() as public:
                public.table("groups").select("id").execute()
            read_data()
        self.assertEqual(len(self.transports), 3)
        self.assertIs(self.requests[0][0], self.requests[-1][0])
        self.assertEqual(self.requests[1][1].headers["apikey"], settings.SUPABASE_SECRET_KEY)
        self.assertNotIn("authorization", self.requests[1][1].headers)
        self.assertNotIn("authorization", self.requests[2][1].headers)

    def test_auth_routes_remain_private_even_on_user_client(self):
        with request_connections():
            with clients.public_client("fixture-user") as client:
                client.table("groups").select("id").execute()
                client.auth.get_user(jwt="fixture-user")
                client.table("plans").select("id").execute()
            self.assertIn(self.requests[1][0], self.closed)
            self.assertNotIn(self.requests[0][0], self.closed)
        self.assertEqual(len(self.transports), 2)
        self.assertIs(self.requests[0][0], self.requests[2][0])
        self.assertIsNot(self.requests[0][0], self.requests[1][0])

    def test_get_user_refresh_and_logout_keep_existing_http_contracts(self):
        with request_connections():
            read_data()
            self.assertEqual(auth_service.get_user("fixture-user"), user_payload()["id"])
            self.assertEqual(auth_service.refresh("fixture-refresh").access_token, "test-access-token")
            auth_service.sign_out("fixture-user")
            read_data()
        self.assertEqual(len(self.transports), 4)
        self.assertEqual([r.url.path for _, r in self.requests],
                         ["/rest/v1/groups", "/auth/v1/user", "/auth/v1/token", "/auth/v1/logout", "/rest/v1/groups"])
        self.assertEqual(self.requests[3][1].url.params["scope"], "local")
        self.assertTrue(all(r.headers["apikey"] == settings.SUPABASE_PUBLISHABLE_KEY for _, r in self.requests))
        self.assertTrue(all(r.headers.get("authorization") != f"Bearer {settings.SUPABASE_PUBLISHABLE_KEY}"
                            for _, r in self.requests))

    def test_no_reuse_outside_request_or_between_requests_of_same_user(self):
        read_data()
        read_data()
        with request_connections():
            read_data()
            read_data()
        with request_connections():
            read_data()
        self.assertEqual(len(self.transports), 4)

    def test_concurrent_requests_for_same_or_different_users_are_isolated(self):
        for tokens in (("same", "same"), ("user-a", "user-b")):
            barrier = Barrier(2)
            def work(token):
                with request_connections():
                    first = read_data(token)
                    pool = _REQUEST_POOL.get()
                    transport = next(iter(pool.transports.values()))
                    barrier.wait(timeout=10)
                    second = read_data(token)
                    self.assertIs(next(iter(pool.transports.values())), transport)
                    self.assertTrue(first.options.httpx_client.is_closed)
                    self.assertTrue(second.options.httpx_client.is_closed)
                    return transport
            with ThreadPoolExecutor(max_workers=2) as workers:
                first, second = list(workers.map(work, tokens))
            self.assertIsNot(first, second)

    def test_inherited_worker_context_does_not_borrow_parent_transport(self):
        with request_connections():
            read_data()
            context = copy_context()
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(context.run, read_data).result(timeout=10)
            read_data()
        self.assertEqual(len(self.transports), 2)
        self.assertIs(self.requests[0][0], self.requests[2][0])
        self.assertIsNot(self.requests[0][0], self.requests[1][0])

    def test_closed_context_cannot_reuse_finished_request(self):
        with request_connections():
            read_data()
            context = copy_context()
        context.run(read_data)
        self.assertEqual(len(self.transports), 2)

    def test_nested_scopes_restore_outer_pool(self):
        with request_connections():
            read_data()
            with request_connections():
                read_data()
            read_data()
        self.assertEqual(len(self.transports), 2)
        self.assertIs(self.requests[0][0], self.requests[2][0])

    def test_client_bound_to_another_active_scope_cannot_borrow_its_pool(self):
        with request_connections():
            with clients.public_client("fixture-user") as client:
                client.table("groups").select("id").execute()
                with request_connections():
                    client.table("groups").select("id").execute()
                    self.assertFalse(_REQUEST_POOL.get().transports)
                client.table("groups").select("id").execute()
        self.assertEqual(len(self.transports), 2)
        self.assertIs(self.requests[0][0], self.requests[2][0])
        self.assertIsNot(self.requests[0][0], self.requests[1][0])

    def test_timeouts_and_network_errors_close_resources_and_reset_scope(self):
        for error in (httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic"),
                      httpx.ConnectTimeout("synthetic")):
            def fail(request):
                raise error
            self.reply = fail
            with self.assertRaises(type(error)), request_connections():
                read_data()
            self.assertIsNone(_REQUEST_POOL.get())

    def test_application_exception_closes_pool(self):
        def view(request):
            read_data()
            raise RuntimeError("synthetic application failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic application failure"):
            DataAPIConnectionsMiddleware(view)(None)
        self.assertTrue(self.closed)

    def test_close_failure_still_closes_other_pools_and_resets_context(self):
        original_exit = httpx.HTTPTransport.__exit__

        def fail_after_close(transport, *args):
            original_exit(transport, *args)
            raise RuntimeError("synthetic close failure")

        with patch.object(httpx.HTTPTransport, "__exit__", fail_after_close):
            with self.assertRaisesRegex(RuntimeError, "synthetic close failure"), request_connections():
                read_data("user-a")
                read_data("user-b")
        self.assertEqual(len(self.closed), 2)
        self.assertIsNone(_REQUEST_POOL.get())

    def test_sdk_construction_failure_does_not_leak_scope_or_client(self):
        with patch.object(clients, "Client", side_effect=RuntimeError("synthetic SDK failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic SDK failure"), request_connections():
                read_data()
        self.assertFalse(self.transports)
        read_data()

    def test_http_errors_are_returned_unchanged_and_pool_is_closed(self):
        self.reply = lambda r: httpx.Response(403, json={"code": "42501", "message": "fixture denied"})
        with request_connections():
            with clients.public_client("fixture-user") as client:
                response = client.options.httpx_client.get(settings.SUPABASE_URL + "/rest/v1/groups")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "42501")

    def test_other_origin_storage_and_changed_credentials_use_private_transports(self):
        headers = {"apikey": settings.SUPABASE_PUBLISHABLE_KEY, "Authorization": "Bearer fixture-user"}
        with request_connections():
            for url, overrides in (("https://other.invalid/rest/v1/groups", {}),
                                   (settings.SUPABASE_URL + "/storage/v1/object", {}),
                                   (settings.SUPABASE_URL + "/rest/v1/groups", {"Authorization": "Bearer other-user"}),
                                   (settings.SUPABASE_URL + "/rest/v1/groups", {"apikey": settings.SUPABASE_SECRET_KEY})):
                with clients.public_client("fixture-user") as client:
                    client.options.httpx_client.get(url, headers={**headers, **overrides})
                self.assertIn(self.requests[-1][0], self.closed)
            self.assertFalse(_REQUEST_POOL.get().transports)
        self.assertEqual(len(self.transports), 4)

    def test_proxy_no_proxy_and_proxy_changes_preserve_httpx_routing(self):
        os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8101"
        with request_connections():
            read_data()
            read_data()
            os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8102"
            read_data()
            os.environ["NO_PROXY"] = "planb-test.invalid"
            read_data()
            read_data()
        self.assertEqual(len(self.transports), 3)
        self.assertEqual([self.transports[i]._pool._proxy_url.port for i in (0, 1)], [8101, 8102])
        self.assertFalse(hasattr(self.transports[2]._pool, "_proxy_url"))

    def test_all_proxy_and_http_only_proxy_preserve_routing(self):
        os.environ["ALL_PROXY"] = "http://proxy.invalid:8101"
        with request_connections():
            read_data()
            read_data()
        self.assertEqual(self.transports[0]._pool._proxy_url.port, 8101)
        del os.environ["ALL_PROXY"]
        os.environ["HTTP_PROXY"] = "http://proxy.invalid:8102"
        with request_connections():
            read_data()
        self.assertFalse(hasattr(self.transports[1]._pool, "_proxy_url"))

    def test_group_role_and_missing_membership_checks_are_preserved(self):
        group_id = "7139c649-18c5-4743-b18d-c891c461e80d"
        role = "owner"

        def reply(request):
            if request.url.path == "/rest/v1/group_members":
                rows = [{"role": role}] if role else []
            else:
                rows = [{"id": group_id, "name": "Fixture", "description": None,
                         "created_by": user_payload()["id"],
                         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}]
            return httpx.Response(200, json=rows)

        self.reply = reply
        with request_connections():
            self.assertEqual(group_service.get_group("fixture-user", group_id)["role"], "owner")
            role = "member"
            self.assertEqual(group_service.get_group("fixture-user", group_id)["role"], "member")
            role = None
            with self.assertRaises(GroupNotFound):
                group_service.get_group("fixture-user", group_id)
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(len(self.requests), 6)
        self.assertTrue(all(r.headers["authorization"] == "Bearer fixture-user" for _, r in self.requests))

    def test_tls_trust_changes_do_not_reuse_existing_pool(self):
        first, second = ssl.create_default_context(), ssl.create_default_context()
        with request_connections(), patch.object(clients, "_tls_context", side_effect=[first, second, second]):
            read_data()
            read_data()
            read_data()
        self.assertEqual(len(self.transports), 2)
        self.assertIs(self.transports[0]._pool._ssl_context, first)
        self.assertIs(self.transports[1]._pool._ssl_context, second)
        self.assertTrue(all(t._pool._ssl_context.check_hostname for t in self.transports))
        self.assertTrue(all(t._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED for t in self.transports))

    def test_methods_queries_body_and_user_jwt_are_unchanged(self):
        with request_connections():
            with clients.public_client("fixture-user") as client:
                client.table("groups").select("id,name").eq("name", "fixture").execute()
            with clients.public_client("fixture-user") as client:
                client.rpc("fixture_write", {"p_value": 7}).execute()
        self.assertEqual(len(self.transports), 1)
        read, write = [request for _, request in self.requests]
        self.assertEqual(read.method, "GET")
        self.assertEqual(read.url.params["select"], "id,name")
        self.assertEqual(read.url.params["name"], "eq.fixture")
        self.assertEqual(write.method, "POST")
        self.assertEqual(write.content, b'{"p_value":7}')
        self.assertEqual(write.headers["authorization"], "Bearer fixture-user")

    def test_before_after_preserves_all_five_http_operations_and_clients(self):
        snapshots, transport_counts, client_counts = [], [], []
        for reuse in (False, True):
            start = len(self.requests)
            http_clients = []
            with request_connections() if reuse else nullcontext():
                auth_service.get_user("fixture-user")
                for table in ("profiles", "groups", "notifications"):
                    client = read_data() if table == "groups" else None
                    if client is None:
                        with clients.public_client("fixture-user") as client:
                            client.table(table).select("id").execute()
                    http_clients.append(client.options.httpx_client)
                with clients.administrative_client() as client:
                    client.table("profiles").select("id").execute()
                http_clients.append(client.options.httpx_client)
            requests = self.requests[start:]
            snapshots.append([(r.method, str(r.url), r.headers.raw, r.content) for _, r in requests])
            transport_counts.append(len({id(t) for t, _ in requests}))
            client_counts.append(len({id(c) for c in http_clients}))
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(len(snapshots[0]), 5)
        self.assertEqual(transport_counts, [5, 3])
        self.assertEqual(client_counts, [4, 4])  # Más el cliente Auth independiente en ambos casos.

    @override_settings(ROOT_URLCONF=__name__)
    def test_django_request_and_template_response_close_pool(self):
        response = self.client.get("/synthetic/")
        self.assertEqual(response.content, b"ok")
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(len(self.closed), 1)
        self.client.get("/synthetic/")
        self.assertEqual(len(self.transports), 2)

    @override_settings(ROOT_URLCONF=__name__)
    def test_template_render_failure_closes_pool(self):
        with self.assertLogs("django.request", level="ERROR"), self.assertRaisesRegex(RuntimeError, "synthetic render failure"):
            self.client.get("/broken/")
        self.assertEqual(len(self.closed), 1)

    @override_settings(ROOT_URLCONF=__name__)
    def test_streaming_work_after_response_uses_private_connections(self):
        response = self.client.get("/stream/")
        self.assertFalse(self.transports)
        self.assertEqual(b"".join(response.streaming_content), b"ok")
        self.assertEqual(len(self.transports), 2)

    @override_settings(ROOT_URLCONF=__name__)
    def test_asgi_requests_do_not_share_transports(self):
        async def run():
            first, second = await asyncio.gather(AsyncClient().get("/synthetic/"), AsyncClient().get("/synthetic/"))
            self.assertEqual(first.content, b"ok")
            self.assertEqual(second.content, b"ok")

        def wakeup_connect(sock, address):
            # Windows usa socketpair TCP local para despertar el event loop.
            # Sólo se habilita ese loopback; HTTP sigue simulado y DNS bloqueado.
            if sock.family == socket.AF_INET and address[0] == "127.0.0.1":
                return _socket.socket.connect(sock, address)
            raise AssertionError("Red externa deshabilitada")

        with patch("socket.socket.connect", wakeup_connect):
            asyncio.run(run())
        self.assertEqual(len(self.transports), 2)
        self.assertEqual(len(self.closed), 2)
