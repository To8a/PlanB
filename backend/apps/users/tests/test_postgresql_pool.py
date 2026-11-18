"""Contratos offline. No sustituyen la integración PostgreSQL del laboratorio."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase
from psycopg_pool import ConnectionPool

from config.postgresql_pool import configure_pool_experiment
from .postgresql_pool_lab import empty_local_schema, local_database, trace_connections


MANAGE_PY = Path(__file__).resolve().parents[3] / "manage.py"


def runtime_settings(extra=None, arguments=None):
    # Configuración real con dotenv y argv controlados: ni .env ni conexiones.
    environment = {
        "DJANGO_SECRET_KEY": "offline-fixture", "DJANGO_DEBUG": "True",
        "SUPABASE_URL": "https://fixture.invalid",
        "SUPABASE_PUBLISHABLE_KEY": "fixture", "SUPABASE_SECRET_KEY": "fixture",
        "DB_NAME": "fixture", "DB_USER": "fixture", "DB_PASSWORD": "fixture",
        "DB_HOST": "fixture.pooler.supabase.com",
    }
    spec = importlib.util.spec_from_file_location("config._pool_settings_fixture", MANAGE_PY.parent / "config" / "settings.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {**environment, **(extra or {})}, clear=True), \
            patch("sys.argv", arguments if arguments is not None else [str(MANAGE_PY), "runserver"]), \
            patch("dotenv.load_dotenv") as dotenv:
        spec.loader.exec_module(module)
        dotenv.assert_called_once()
    return module


def database_config():
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "planb_pool_test_offline",
        "USER": "fixture", "PASSWORD": "fixture",
        "HOST": "127.0.0.1", "PORT": "5432",
        "OPTIONS": {"sslmode": "require", "options": "-c search_path=django_internal"},
    }


class PoolConfigurationTests(SimpleTestCase):
    databases = {"default"}  # El préstamo nativo se simula; la suite sólo habilita SQLite.

    def test_disabled_preserves_configuration_without_loading_pool(self):
        original = database_config()
        with patch("psycopg_pool.ConnectionPool", side_effect=AssertionError("pool desactivado")):
            self.assertIs(configure_pool_experiment(original), original)
            connection = ConnectionHandler({"default": original})["default"]
            self.assertIsNone(connection.pool)
            self.assertEqual(connection.settings_dict["CONN_MAX_AGE"], 0)
            self.assertFalse(connection.settings_dict["CONN_HEALTH_CHECKS"])

    def test_enabled_preserves_endpoint_tls_schema_and_input(self):
        original = database_config()
        before = deepcopy(original)
        configured = configure_pool_experiment(original, enabled=True, local=True)
        self.assertEqual(original, before)
        for key in ("ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT"):
            self.assertEqual(configured[key], before[key])
        for key in ("sslmode", "options"):
            self.assertEqual(configured["OPTIONS"][key], before["OPTIONS"][key])
        self.assertEqual(configured["CONN_MAX_AGE"], 0)
        self.assertTrue(configured["CONN_HEALTH_CHECKS"])

    def test_rejects_nonlocal_nonpostgresql_and_persistent_connections(self):
        for config, local in (
            (database_config(), False),
            ({**database_config(), "ENGINE": "django.db.backends.sqlite3"}, True),
            ({**database_config(), "CONN_MAX_AGE": 60}, True),
        ):
            with self.subTest(local=local, engine=config["ENGINE"]):
                with self.assertRaises(ImproperlyConfigured):
                    configure_pool_experiment(config, enabled=True, local=local)

    def test_normal_local_runserver_enables_pool_without_extra_variables(self):
        configured = runtime_settings()
        database = configured.DATABASES["default"]
        self.assertIn("pool", database["OPTIONS"])
        self.assertEqual(database["HOST"], "fixture.pooler.supabase.com")
        self.assertEqual(database["PORT"], "5432")
        self.assertEqual(database["CONN_MAX_AGE"], 0)
        self.assertTrue(database["CONN_HEALTH_CHECKS"])
        self.assertTrue(configured.DEBUG)

    def test_explicit_false_overrides_automatic_activation(self):
        for value in ("false", "False", "0", "no", "off", ""):
            with self.subTest(value=value):
                configured = runtime_settings({"PLANB_PG_POOL_EXPERIMENT": value})
                database = configured.DATABASES["default"]
                self.assertNotIn("pool", database["OPTIONS"])
                self.assertNotIn("CONN_HEALTH_CHECKS", database)
                self.assertTrue(configured.DEBUG)

    def test_production_vercel_ci_and_public_urls_exclude_activation(self):
        from config.postgresql_pool import CI_MARKERS
        cases = [{"DJANGO_DEBUG": "False"}, {"VERCEL": "1"}, {"VERCEL": "false"},
                 {"DJANGO_PUBLIC_URL": "https://planb.example"}]
        cases += [{key: "true"} for key in CI_MARKERS]
        cases += [{"CI": "false"}, {"VERCEL": ""}]
        for extra in cases:
            with self.subTest(extra=extra):
                configured = runtime_settings(extra)
                self.assertNotIn("pool", configured.DATABASES["default"]["OPTIONS"])
                self.assertEqual(configured.DEBUG, extra.get("DJANGO_DEBUG") != "False")
                self.assertEqual(configured.SESSION_COOKIE_SECURE, not configured.DEBUG)
                with self.assertRaises(ImproperlyConfigured):
                    runtime_settings({**extra, "PLANB_PG_POOL_EXPERIMENT": "true"})

    def test_other_commands_and_wsgi_asgi_do_not_automatically_enable_pool(self):
        arguments = [[str(MANAGE_PY), command] for command in
                     ("check", "test", "migrate", "collectstatic", "shell")]
        arguments += [["gunicorn", "config.wsgi:application"], ["uvicorn", "config.asgi:application"],
                      ["gunicorn", "runserver"], ["pytest"], ["-c"]]
        for argv in arguments:
            with self.subTest(argv=argv):
                self.assertNotIn("pool", runtime_settings(arguments=argv).DATABASES["default"]["OPTIONS"])

    def test_local_bindings_and_autoreloader_are_supported(self):
        for arguments in ([], ["8001"], ["127.0.0.1:8001"], ["localhost:8001"],
                          ["[::1]:8001"], ["--ipv6"], ["--noreload", "--nothreading"]):
            for extra in ({}, {"RUN_MAIN": "true"}):
                with self.subTest(arguments=arguments, extra=extra):
                    configured = runtime_settings(extra, [str(MANAGE_PY), "runserver", *arguments])
                    self.assertIn("pool", configured.DATABASES["default"]["OPTIONS"])

    def test_public_bindings_and_unknown_options_fail_closed(self):
        for arguments in (["0.0.0.0:8000"], ["[::]:8000"], ["192.168.1.2:8000"],
                          ["example.com:8000"], ["8000", "8001"], ["invalid"], ["70000"],
                          ["--settings=config.settings"], ["--unknown-option"]):
            with self.subTest(arguments=arguments):
                configured = runtime_settings(arguments=[str(MANAGE_PY), "runserver", *arguments])
                self.assertNotIn("pool", configured.DATABASES["default"]["OPTIONS"])

    def test_test_settings_ignore_runserver_and_explicit_enable_flag(self):
        from config import settings_test
        with patch.dict(os.environ, {"PLANB_PG_POOL_EXPERIMENT": "true"}), \
                patch("sys.argv", [str(MANAGE_PY), "runserver"]):
            self.assertEqual(settings_test.DATABASES["default"]["ENGINE"], "django.db.backends.sqlite3")
            self.assertNotIn("pool", settings_test.DATABASES["default"].get("OPTIONS", {}))

    def test_manage_launcher_keeps_normal_runserver_arguments(self):
        spec = importlib.util.spec_from_file_location("_pool_manage_fixture", MANAGE_PY)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        observed = []

        def execute(arguments):
            self.assertEqual(os.environ["DJANGO_SETTINGS_MODULE"], "config.settings")
            observed.append(runtime_settings(arguments=arguments).DATABASES["default"])

        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", [str(MANAGE_PY), "runserver"]), \
                patch("django.core.management.execute_from_command_line", side_effect=execute):
            launcher.main()
        self.assertIn("pool", observed[0]["OPTIONS"])

    def test_native_pool_is_lazy_bounded_and_keeps_connection_parameters(self):
        config = configure_pool_experiment(database_config(), enabled=True, local=True)
        first = ConnectionHandler({"default": config})["default"]
        second = DatabaseWrapper(first.settings_dict, alias="default")
        try:
            pool = first.pool
            self.assertIsInstance(pool, ConnectionPool)
            self.assertIs(second.pool, pool)
            self.assertTrue(pool.closed)
            self.assertEqual((pool.min_size, pool.max_size), (0, 2))
            self.assertEqual((pool.timeout, pool.max_waiting), (3.0, 4))
            self.assertEqual((pool.max_idle, pool.max_lifetime), (60.0, 300.0))
            self.assertEqual(pool.reconnect_timeout, 5.0)
            self.assertIs(pool._check, ConnectionPool.check_connection)
            self.assertEqual(pool.kwargs["options"], "-c search_path=django_internal")
            self.assertEqual(pool.kwargs["sslmode"], "require")
            self.assertIsNone(pool.kwargs["prepare_threshold"])
            self.assertTrue(pool.kwargs["autocommit"])
        finally:
            first.close_pool()

    def test_django_borrows_and_returns_to_native_pool_without_physical_close(self):
        config = configure_pool_experiment(database_config(), enabled=True, local=True)
        connection = ConnectionHandler({"default": config})["default"]
        pool = connection.pool
        physical = Mock()
        physical._pool = pool
        try:
            with patch.object(pool, "open") as opened, \
                    patch.object(pool, "getconn", return_value=physical) as borrowed, \
                    patch.object(pool, "putconn") as returned:
                connection.ensure_connection()
                self.assertIs(connection.connection, physical)
                opened.assert_called_once()
                borrowed.assert_called_once()
                connection.close()
                returned.assert_called_once_with(physical)
                physical.close.assert_not_called()
                self.assertIsNone(connection.connection)
        finally:
            connection.close_pool()


class PoolLaboratorySafetyTests(SimpleTestCase):
    def test_lab_only_builds_loopback_connections_to_disposable_names(self):
        for name in ("postgres", "production", "planb_pool_test_x;DROP", "planb_pool_test_á"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                local_database(name, 5432, "fixture", "fixture")
        database = local_database("planb_pool_test_fixture", 5432, "fixture", "fixture")
        self.assertEqual(database["HOST"], "127.0.0.1")
        self.assertEqual(database["OPTIONS"]["hostaddr"], "127.0.0.1")
        self.assertEqual(database["OPTIONS"]["connect_timeout"], 3)

    def test_lab_rejects_existing_schema_or_tables_before_any_ddl(self):
        database = local_database("planb_pool_test_fixture", 5432, "fixture", "fixture")
        with patch("psycopg.connect") as connect:
            control = connect.return_value.__enter__.return_value
            control.execute.return_value.fetchone.return_value = (True,)
            with self.assertRaises(ValueError), empty_local_schema(database):
                self.fail("No debe entrar al laboratorio")
            self.assertEqual(control.execute.call_count, 1)
            self.assertTrue(control.execute.call_args.args[0].startswith("SELECT"))

    def test_metrics_distinguish_opened_connections_from_failed_attempts(self):
        with patch("psycopg.Connection.connect", side_effect=[Mock(), OSError("fixture")]):
            with trace_connections() as (metrics, lock):
                from psycopg import Connection
                Connection.connect()
                with self.assertRaises(OSError):
                    Connection.connect()
            self.assertEqual(metrics["physical_opens"], 1)
            self.assertEqual(metrics["connect_errors"], 1)
            self.assertEqual(metrics["loans"], 0)

    def test_metrics_cover_module_alias_used_by_django_without_double_counting(self):
        import psycopg
        with patch("psycopg.connect", return_value=Mock()) as direct, \
                patch("psycopg.Connection.connect", return_value=Mock()) as through_class:
            with trace_connections() as (metrics, lock):
                psycopg.connect()
                psycopg.Connection.connect()
            direct.assert_called_once()
            through_class.assert_called_once()
            self.assertEqual(metrics["physical_opens"], 2)
            self.assertEqual(metrics["module_opens"], 1)
            self.assertEqual(metrics["class_opens"], 1)

    def test_reuse_is_observed_from_the_same_physical_connection(self):
        physical = Mock()
        pool = ConnectionPool(min_size=0, max_size=2, open=False)
        with patch.object(ConnectionPool, "getconn", return_value=physical):
            with trace_connections() as (metrics, lock):
                self.assertIs(pool.getconn(), physical)
                self.assertIs(pool.getconn(), physical)
            self.assertEqual(metrics["loans"], 2)
            self.assertEqual(metrics["reused_loans"], 1)
            self.assertEqual(metrics["physical_opens"], 0)
        pool.close()
