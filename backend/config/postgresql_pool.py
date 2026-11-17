"""Configuración opt-in del experimento local; no crea conexiones ni pools."""
from django.core.exceptions import ImproperlyConfigured


def configure_pool_experiment(database, *, enabled=False, local=False):
    if not enabled:
        return database
    if not local:
        raise ImproperlyConfigured(
            "PLANB_PG_POOL_EXPERIMENT sólo está permitido en desarrollo local "
            "con DJANGO_DEBUG=True y fuera de Vercel."
        )
    if database["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("El experimento requiere PostgreSQL.")
    if database.get("CONN_MAX_AGE", 0) != 0:
        raise ImproperlyConfigured("El experimento requiere CONN_MAX_AGE=0.")
    return {
        **database,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            **database.get("OPTIONS", {}),
            "pool": {
                "min_size": 0,
                "max_size": 2,
                "timeout": 3.0,
                "max_waiting": 4,
                "max_idle": 60.0,
                "max_lifetime": 300.0,
                "reconnect_timeout": 5.0,
            },
        },
    }
