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

    def test_runtime_flag_defaults_off_and_rejects_vercel_and_production(self):
        # Ejecutar settings con dotenv simulado: nunca leer el .env del proyecto.
        environment = {
            "DJANGO_SECRET_KEY": "offline-fixture", "DJANGO_DEBUG": "True",
            "SUPABASE_URL": "https://fixture.invalid",
            "SUPABASE_PUBLISHABLE_KEY": "fixture", "SUPABASE_SECRET_KEY": "fixture",
            "DB_NAME": "fixture", "DB_USER": "fixture", "DB_PASSWORD": "fixture",
            "DB_HOST": "127.0.0.1",
        }

        def load(extra):
            path = Path(__file__).resolve().parents[3] / "config" / "settings.py"
            spec = importlib.util.spec_from_file_location("config._pool_settings_fixture", path)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {**environment, **extra}, clear=True), \
                    patch("dotenv.load_dotenv") as dotenv:
                spec.loader.exec_module(module)
                dotenv.assert_called_once()
            return module.DATABASES["default"]

        self.assertNotIn("pool", load({})["OPTIONS"])
        self.assertIn("pool", load({"PLANB_PG_POOL_EXPERIMENT": "true"})["OPTIONS"])
        for extra in ({"DJANGO_DEBUG": "False"}, {"VERCEL": "1"}):
            with self.subTest(extra=extra), self.assertRaises(ImproperlyConfigured):
                load({"PLANB_PG_POOL_EXPERIMENT": "true", **extra})

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
