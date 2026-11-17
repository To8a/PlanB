"""Laboratorio opt-in con PostgreSQL loopback desechable y Auth ficticio.

No carga .env ni settings de ejecución, no aplica migraciones y nunca usa Supabase.
Ver docs/testing/postgresql-pooling.md antes de ejecutar.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
from importlib.metadata import version
from math import ceil
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from threading import Barrier, Event, Lock
from time import monotonic, perf_counter, sleep
import unittest
from unittest.mock import patch


def local_database(name, port, user, password):
    if not name.startswith("planb_pool_test_") or not name.isascii() or not name.replace("_", "").isalnum():
        raise ValueError("La base desechable debe llamarse planb_pool_test_<identificador>.")
    if not 1 <= port <= 65535:
        raise ValueError("Puerto inválido.")
    return {
        "ENGINE": "django.db.backends.postgresql", "NAME": name,
        "USER": user, "PASSWORD": password, "HOST": "127.0.0.1", "PORT": str(port),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "hostaddr": "127.0.0.1", "sslmode": "disable", "connect_timeout": 3,
            "options": "-c search_path=django_internal -c statement_timeout=5000 -c lock_timeout=3000",
        },
    }


@contextmanager
def empty_local_schema(database):
    """Rechaza bases con tablas de usuario; elimina sólo el esquema que creó."""
    import psycopg
    params = dict(database["OPTIONS"])
    params.update(dbname=database["NAME"], host="127.0.0.1", hostaddr="127.0.0.1",
                  port=database["PORT"], user=database["USER"], password=database["PASSWORD"])
    with psycopg.connect(**params, autocommit=True) as control:
        if control.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'django_internal') "
            "OR EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'f'))"
        ).fetchone()[0]:
            raise ValueError("El laboratorio requiere una base vacía sin django_internal.")
        control.execute("CREATE SCHEMA django_internal")
        try:
            yield control
        finally:
            # La conexión ya está fijada a la base local validada. Nunca usar un
            # nombre obtenido del entorno para construir un DROP.
            control.execute("DROP SCHEMA django_internal CASCADE")
            if control.execute("SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'django_internal')").fetchone()[0]:
                raise AssertionError("El esquema del laboratorio no se eliminó")


@contextmanager
def django_resources(monitor=None):
    """Cierra recursos aun ante excepciones, sin crear otro pool al inspeccionarlo."""
    from django.db import connections
    from django.db.backends.postgresql.base import DatabaseWrapper
    try:
        yield
    finally:
        connections.close_all()
        for alias, pool in list(DatabaseWrapper._connection_pools.items()):
            pool.close()
            del DatabaseWrapper._connection_pools[alias]
        if monitor is not None and monitor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()[0]:
            raise AssertionError("Quedaron conexiones retenidas en el laboratorio")


class PoolIntegrationTests(unittest.TestCase):
    def wait_for_database_lock(self):
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if self.monitor.execute(
                "SELECT EXISTS (SELECT FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock')"
            ).fetchone()[0]:
                return True
            sleep(0.005)
        return False

    def setUp(self):
        from django.contrib.sessions.models import Session
        from django.db import connections
        from apps.users.models import SupabaseSession
        connections.close_all()
        SupabaseSession.objects.all().delete()
        Session.objects.all().delete()
        connections.close_all()

    def tearDown(self):
        from django.db import connections
        connections.close_all()

    def session(self, identity="user-a", expired=False):
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory
        from django.utils import timezone
        from apps.users.services import session_service
        from apps.users.services.types import AuthSession
        request = RequestFactory().get("/_pool_fixture/")
        request.session = SessionStore()
        session_service.establish_session(request, AuthSession(
            access_token="access-" + identity, refresh_token="refresh-" + identity,
            expires_at=int((timezone.now() + timedelta(seconds=-1 if expired else 3600)).timestamp()),
            user_id=identity,
        ))
        key = request.session.session_key
        from django.db import connections
        connections.close_all()
        return key

    def request(self, key):
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory
        request = RequestFactory().get("/_pool_fixture/")
        request.session = SessionStore(key)
        return request

    def worker(self, operation):
        from django.db import connections
        try:
            return operation()
        finally:
            connections.close_all()

    def test_two_users_and_same_user_concurrently(self):
        from apps.users.services import session_service
        keys = [self.session("user-a"), self.session("user-b")]
        for pair in (keys, [keys[0], keys[0]]):
            barrier = Barrier(2)

            def read(key):
                barrier.wait(timeout=5)
                request = self.request(key)
                # Repetir en la misma solicitud también ejercita revalidación.
                return (session_service.get_access_token(request), session_service.get_access_token(request))

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(self.worker, lambda key=key: read(key)) for key in pair]
                actual = [future.result(timeout=10) for future in futures]
            expected = ["access-user-a", "access-user-b" if pair[1] == keys[1] else "access-user-a"]
            self.assertEqual(actual, [(token, token) for token in expected])

    def renewed(self):
        from django.utils import timezone
        from apps.users.services.types import AuthSession
        return AuthSession(access_token="access-new", refresh_token="refresh-new",
                           expires_at=int((timezone.now() + timedelta(hours=1)).timestamp()), user_id="user-a")

    def test_concurrent_refresh_rotates_once(self):
        from apps.users.models import SupabaseSession
        from apps.users.services import session_service
        key = self.session(expired=True)
        barrier = Barrier(2)

        def read():
            barrier.wait(timeout=5)
            return session_service.get_access_token(self.request(key))

        with patch.object(session_service.auth_service, "refresh", return_value=self.renewed()) as refresh:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(self.worker, read) for _ in range(2)]
                self.assertEqual([future.result(timeout=10) for future in futures], ["access-new"] * 2)
            refresh.assert_called_once_with("refresh-user-a")
        stored = SupabaseSession.objects.get(pk=key)
        self.assertEqual((stored.access_token, stored.refresh_token), ("access-new", "refresh-new"))

    def test_refresh_and_logout_in_both_lock_orders(self):
        from django.contrib.sessions.models import Session
        from apps.users.models import SupabaseSession
        from apps.users.services import session_service
        for logout_first in (False, True):
            key = self.session(expired=not logout_first)
            entered, release, second_started = Event(), Event(), Event()

            def hold(result=None):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("No se liberó la barrera del proveedor ficticio")
                return result

            def read():
                return session_service._active_token(self.request(key))

            def logout():
                session_service.logout(self.request(key))

            first, second = (logout, read) if logout_first else (read, logout)

            def run_second():
                second_started.set()
                return second()

            with patch.object(session_service.auth_service, "refresh",
                              side_effect=lambda token: hold(self.renewed())) as refresh, \
                    patch.object(session_service.auth_service, "sign_out",
                                 side_effect=(lambda token: hold()) if logout_first else None):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(self.worker, first)
                    try:
                        self.assertTrue(entered.wait(timeout=5))
                        second_future = executor.submit(self.worker, run_second)
                        self.assertTrue(second_started.wait(timeout=5))
                        self.assertTrue(self.wait_for_database_lock(), "La segunda solicitud debe esperar el lock real")
                    finally:
                        release.set()
                    first_future.result(timeout=10)
                    result = second_future.result(timeout=10)
                if logout_first:
                    self.assertIsNone(result)
                    refresh.assert_not_called()
            self.assertFalse(SupabaseSession.objects.filter(pk=key).exists())
            self.assertFalse(Session.objects.filter(pk=key).exists())

    def test_expiration_and_failed_refresh_invalidate_session(self):
        from django.contrib.sessions.models import Session
        from django.utils import timezone
        from apps.users.models import SupabaseSession
        from apps.users.services import session_service
        from apps.users.services.exceptions import ServiceUnavailable
        key = self.session()
        Session.objects.filter(pk=key).update(expire_date=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(session_service._active_token(self.request(key)))
        key = self.session(expired=True)
        with patch.object(session_service.auth_service, "refresh", side_effect=ServiceUnavailable()):
            self.assertIsNone(session_service._active_token(self.request(key)))
        self.assertFalse(SupabaseSession.objects.filter(pk=key).exists())
        self.assertFalse(Session.objects.filter(pk=key).exists())

    def test_schema_rollback_and_reuse_leave_no_open_transaction(self):
        from django.db import connection, transaction
        from psycopg.pq import TransactionStatus
        for _ in range(3):
            with connection.cursor() as cursor:
                cursor.execute("SHOW search_path")
                self.assertEqual(cursor.fetchone()[0], "django_internal")
                cursor.execute("SELECT current_schema()")
                self.assertEqual(cursor.fetchone()[0], "django_internal")
            with self.assertRaisesRegex(RuntimeError, "rollback-fixture"):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                    raise RuntimeError("rollback-fixture")
            self.assertTrue(connection.get_autocommit())
            self.assertEqual(connection.connection.info.transaction_status, TransactionStatus.IDLE)
            physical = connection.connection
            pooled = bool(connection.pool)
            connection.close()
            self.assertIsNone(connection.connection)
            if not pooled:
                self.assertTrue(physical.closed)

    def test_pool_returns_idle_connections_and_closes_resources(self):
        from django.db import connection
        from psycopg.pq import TransactionStatus
        if not connection.pool:
            self.skipTest("Sólo corresponde a la variante con pooling")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        connection.close()
        pool = connection.pool
        physical = pool.getconn(timeout=3)
        try:
            self.assertTrue(physical.autocommit)
            self.assertEqual(physical.info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual(physical.execute("SHOW search_path").fetchone()[0], "django_internal")
        finally:
            pool.putconn(physical)
        self.assertEqual(pool.get_stats()["requests_waiting"], 0)
        connection.close_pool()
        self.assertTrue(pool.closed)
        self.assertTrue(physical.closed)

    def test_broken_idle_and_inflight_connections_recover(self):
        from django.db import Error, connection, transaction
        connection.ensure_connection()
        physical = connection.connection
        connection.close()
        physical.close()  # Interrupción real de una conexión del laboratorio.
        connection.ensure_connection()
        self.assertIsNot(connection.connection, physical)
        with self.assertRaises(Error):
            with transaction.atomic():
                connection.connection.close()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
        connection.close()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_pool_saturation_timeout_and_queue_limit(self):
        from django.db import connection
        from django.db.backends.postgresql.base import DatabaseWrapper
        from psycopg_pool import PoolTimeout, TooManyRequests
        if not connection.pool:
            self.skipTest("Sólo corresponde a la variante con pooling")
        pool = connection.pool
        holders = [DatabaseWrapper(connection.settings_dict, alias="default") for _ in range(2)]
        try:
            for holder in holders:
                holder.ensure_connection()
            with self.assertRaises(PoolTimeout):
                pool.getconn(timeout=0.05)
            # En 3.3.3 el waiter vencido permanece hasta devolver una conexión.
            # No confundir requests_waiting con cuatro waiters todavía activos.
            self.assertEqual(pool.get_stats()["requests_waiting"], 1)
            holders[0].close()
            holders[0].ensure_connection()
            self.assertEqual(pool.get_stats()["requests_waiting"], 0)

            def wait_for_connection():
                physical = pool.getconn(timeout=3)
                try:
                    return physical.info.transaction_status.name
                finally:
                    pool.putconn(physical)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(wait_for_connection) for _ in range(4)]
                try:
                    deadline = monotonic() + 2
                    while pool.get_stats()["requests_waiting"] < 4 and monotonic() < deadline:
                        sleep(0.005)
                    self.assertEqual(pool.get_stats()["requests_waiting"], 4)
                    with self.assertRaises(TooManyRequests):
                        pool.getconn(timeout=0.05)
                finally:
                    for holder in holders:
                        holder.close()
                for future in futures:
                    self.assertEqual(future.result(timeout=5), "IDLE")
            self.assertEqual(pool.get_stats()["requests_waiting"], 0)
            self.assertEqual(pool.get_stats()["pool_available"], 2)
            with holders[0].cursor() as cursor:
                cursor.execute("SELECT 1")
                self.assertEqual(cursor.fetchone()[0], 1)
        finally:
            for holder in holders:
                holder.close()

    def test_resources_close_even_on_failure(self):
        from django.db import connection
        with self.assertRaisesRegex(RuntimeError, "cleanup-fixture"), django_resources(self.monitor):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            physical = connection.connection
            raise RuntimeError("cleanup-fixture")
        self.assertIsNone(connection.connection)
        self.assertTrue(physical.closed)
        self.assertEqual(self.monitor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()[0], 0)


@contextmanager
def trace_connections():
    """Mide aperturas físicas; connection_created no sirve para contarlas."""
    import psycopg
    from psycopg_pool import ConnectionPool
    lock = Lock()
    metrics = {"physical_opens": 0, "physical_open_ms": 0.0, "connect_errors": 0, "loans": 0,
               "module_opens": 0, "class_opens": 0, "reused_loans": 0, "acquire_errors": 0,
               "acquire_ms": 0.0, "health_checks": 0, "health_ms": 0.0,
               "commits": 0, "commit_ms": 0.0}
    module_connect = psycopg.connect  # Alias enlazado que usa Django sin pooling.
    class_connect = psycopg.Connection.connect  # Ruta utilizada por psycopg_pool.
    getconn = ConnectionPool.getconn
    check = ConnectionPool.check_connection
    commit = psycopg.Connection.commit
    loaned_connections = []  # Retener referencias impide confundir ids reciclados.

    def open_connection(connect, source, *args, **kwargs):
        start = perf_counter()
        try:
            physical = connect(*args, **kwargs)
        except Exception:
            with lock:
                metrics["connect_errors"] += 1
            raise
        else:
            with lock:
                metrics["physical_opens"] += 1
                metrics[source] += 1
            return physical
        finally:
            with lock:
                metrics["physical_open_ms"] += (perf_counter() - start) * 1000

    def borrow(pool, *args, **kwargs):
        start = perf_counter()
        try:
            physical = getconn(pool, *args, **kwargs)
        except Exception:
            with lock:
                metrics["acquire_errors"] += 1
            raise
        else:
            with lock:
                metrics["loans"] += 1
                if any(previous is physical for previous in loaned_connections):
                    metrics["reused_loans"] += 1
                else:
                    loaned_connections.append(physical)
            return physical
        finally:
            with lock:
                metrics["acquire_ms"] += (perf_counter() - start) * 1000

    def timed(operation, counter, duration, *args, **kwargs):
        start = perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            with lock:
                metrics[counter] += 1
                metrics[duration] += (perf_counter() - start) * 1000

    with patch.object(psycopg, "connect", side_effect=lambda *a, **kw: open_connection(
            module_connect, "module_opens", *a, **kw)), \
            patch.object(psycopg.Connection, "connect", side_effect=lambda *a, **kw: open_connection(
                class_connect, "class_opens", *a, **kw)), \
            patch.object(ConnectionPool, "getconn", new=borrow), \
            patch.object(ConnectionPool, "check_connection", new=staticmethod(lambda conn: timed(
                check, "health_checks", "health_ms", conn))), \
            patch.object(psycopg.Connection, "commit", new=lambda self: timed(
                commit, "commits", "commit_ms", self)):
        yield metrics, lock


def summarize(rows):
    durations = ("response_ms", "physical_open_ms", "acquire_ms", "pool_wait_ms", "health_ms",
                 "session_sql_ms", "active_token_ms", "commit_ms")
    counts = ("physical_opens", "module_opens", "class_opens", "loans", "reused_loans",
              "health_checks", "connect_errors", "acquire_errors", "commits", "errors")
    later = [row for row in rows if row["phase"] == "later"]
    return {
        "requests": len(rows), "later_requests": len(later),
        "totals": {key: sum(row[key] for row in rows) for key in counts},
        "total_ms": {key: sum(row[key] for row in rows) for key in durations},
        "later_median_ms": {key: median(row[key] for row in later) for key in durations},
        "later_p95_ms": {key: sorted(row[key] for row in later)[ceil(len(later) * .95) - 1] for key in durations},
    }


def benchmark(samples):
    from django.conf import settings
    from django.core.handlers.wsgi import WSGIHandler
    from django.db import connections
    from django.http import HttpResponse
    from django.test import RequestFactory, override_settings
    from django.urls import path
    from apps.users.services import session_service

    fixture = PoolIntegrationTests()
    fixture.setUp()
    key = fixture.session()
    connections.close_all()
    if connections["default"].pool:
        connections["default"].close_pool()  # La muestra 1 incluye arranque frío.

    def view(request):
        if session_service.current_user(request) is None:
            return HttpResponse(status=401)
        for _ in range(2):
            if session_service.get_access_token(request) != "access-user-a":
                return HttpResponse(status=403)
        return HttpResponse("fixture-ok")

    rows = []
    active_token = session_service._active_token
    with trace_connections() as (metrics, lock), \
            patch.object(session_service.auth_service, "get_user", return_value="user-a"), \
            patch.object(session_service.profile_service, "get_profile", return_value={"id": "user-a"}), \
            override_settings(ROOT_URLCONF=type("LabURLs", (), {"urlpatterns": [path("_pool_fixture/", view)]})):
        handler = WSGIHandler()
        for sample in range(1, samples + 1):
            with lock:
                before = metrics.copy()
            connection = connections["default"]
            if connection.connection is not None:
                raise AssertionError("La muestra comenzó con un préstamo/conexión de Django abierto")
            stats_before = connection.pool.get_stats() if connection.pool else {}
            sql_ms, sql_count, token_ms = 0.0, 0, 0.0
            physical_connections = []
            backend_pids = set()

            def token_timer(request):
                nonlocal token_ms
                start = perf_counter()
                try:
                    return active_token(request)
                finally:
                    token_ms += (perf_counter() - start) * 1000

            def sql_timer(execute, sql, params, many, context):
                nonlocal sql_ms, sql_count
                start = perf_counter()
                try:
                    return execute(sql, params, many, context)
                finally:
                    sql_ms += (perf_counter() - start) * 1000
                    sql_count += 1
                    physical = context["connection"].connection
                    if physical is not None and not any(previous is physical for previous in physical_connections):
                        physical_connections.append(physical)
                        backend_pids.add(physical.info.backend_pid)

            environ = RequestFactory().get("/_pool_fixture/", HTTP_COOKIE=f"{settings.SESSION_COOKIE_NAME}={key}").environ
            status = []
            body, error_type = b"", None
            start = perf_counter()
            try:
                with connection.execute_wrapper(sql_timer), \
                        patch.object(session_service, "_active_token", side_effect=token_timer):
                    response = handler(environ, lambda code, headers: status.append(code))
                    try:
                        body = b"".join(response)
                    finally:
                        response.close()
            except Exception as error:
                error_type = type(error).__name__  # Nunca registrar mensaje, SQL ni credenciales.
                connections.close_all()
            response_ms = (perf_counter() - start) * 1000
            stats_after = connection.pool.get_stats() if connection.pool else {}
            with lock:
                delta = {name: metrics[name] - before[name] for name in metrics}
            row = {"sample": sample, "phase": "first" if sample == 1 else "later",
                   **delta, "pool_wait_ms": stats_after.get("requests_wait_ms", 0) - stats_before.get("requests_wait_ms", 0),
                   "session_sql_ms": sql_ms, "session_sql_count": sql_count, "active_token_ms": token_ms,
                   "response_ms": response_ms, "error_type": error_type,
                   "backend_pids": sorted(backend_pids),
                   "connection_released": connection.connection is None,
                   "errors": int(error_type is not None or status != ["200 OK"] or body != b"fixture-ok")}
            # Guardas de validez: no publicar un baseline SQL con aperturas cero.
            if not row["errors"]:
                if not row["connection_released"] or len(physical_connections) != 1:
                    raise AssertionError("Ciclo de conexión inesperado en la medición")
                if connection.pool:
                    if row["loans"] != 1 or (sample == 1 and row["physical_opens"] < 1):
                        raise AssertionError("La instrumentación no observó el préstamo/apertura del pool")
                elif row["physical_opens"] != 1 or not physical_connections[0].closed:
                    raise AssertionError("La instrumentación no observó apertura y cierre del baseline")
            rows.append(row)
        # Cerrar los workers dentro de la instrumentación para no dejar actividad pendiente.
        connections.close_all()
        if connections["default"].pool:
            connections["default"].close_pool()
    if any(row["errors"] for row in rows):
        raise AssertionError("El benchmark recibió respuestas inesperadas")
    return {"benchmark": "WSGI-in-process; Auth y datos ficticios; PostgreSQL loopback real",
            "samples": rows, "summary": summarize(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Base VACÍA y desechable planb_pool_test_<id>")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--mode", choices=("baseline", "pool", "compare"), default="compare")
    parser.add_argument("--samples", type=int, default=21)
    parser.add_argument("--output", type=Path, help="Guardar resultados JSON verificables (sin credenciales)")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("Se requieren al menos dos muestras")
    database = local_database(args.database, args.port, args.user, os.getenv("PLANB_POOL_TEST_PASSWORD", ""))
    if args.mode == "compare":
        # Dos procesos: ni conexiones ni settings sobreviven entre variantes.
        runs = []
        for mode in ("baseline", "pool", "pool", "baseline"):
            print(f"Variante {mode}", file=sys.stderr, flush=True)
            completed = subprocess.run([sys.executable, "-B", "-m", __spec__.name,
                            "--database", args.database, "--port", str(args.port), "--user", args.user,
                            "--samples", str(args.samples), "--mode", mode], capture_output=True, text=True)
            print(completed.stderr, file=sys.stderr, end="", flush=True)
            completed.check_returncode()
            runs.append(json.loads(completed.stdout))
        report = {"runs": runs, "comparison": {
            mode: summarize([row for run in runs if run["mode"] == mode for row in run["samples"]])
            for mode in ("baseline", "pool")}}
        emit(report, args.output)
        return
    from config import settings_test
    from config.postgresql_pool import configure_pool_experiment
    settings_test.DATABASES = {"default": configure_pool_experiment(database, enabled=args.mode == "pool", local=True)}
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_test"
    import django
    django.setup()
    from django.contrib.sessions.models import Session
    from django.db import connections
    from apps.users.models import SupabaseSession
    with empty_local_schema(database) as monitor:
        PoolIntegrationTests.monitor = monitor
        with django_resources(monitor):
            # Fixtures del laboratorio, sin migrate ni tabla django_migrations.
            with connections["default"].schema_editor() as editor:
                editor.create_model(Session)
                editor.create_model(SupabaseSession)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(PoolIntegrationTests)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful():
                raise SystemExit(1)
            report = benchmark(args.samples)
            report.update(mode=args.mode, tests={"run": result.testsRun,
                "passed": result.testsRun - len(result.skipped), "skipped": len(result.skipped),
                "failures": len(result.failures), "errors": len(result.errors)})
        configured = settings_test.DATABASES["default"]
        report["configuration"] = {
            "host": configured["HOST"], "port": configured["PORT"],
            "conn_max_age": configured["CONN_MAX_AGE"],
            "health_checks": configured.get("CONN_HEALTH_CHECKS", False),
            "sslmode": configured["OPTIONS"]["sslmode"], "options": configured["OPTIONS"]["options"],
            "pool": configured["OPTIONS"].get("pool"),
        }
        report["versions"] = {name: version(name) for name in ("Django", "psycopg", "psycopg-pool")}
        report["server_version"] = monitor.info.server_version
    report["cleanup_verified"] = True
    emit(report, args.output)


def emit(report, output):
    text = json.dumps(report, indent=2)
    if output is not None:
        output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
