"""Pooling de desarrollo local; detección conservadora, sin abrir conexiones."""
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


CI_MARKERS = (
    "CI", "GITHUB_ACTIONS", "GITLAB_CI", "TF_BUILD", "JENKINS_URL",
    "TEAMCITY_VERSION", "CIRCLECI", "TRAVIS", "BUILDKITE", "DRONE",
    "BITBUCKET_BUILD_NUMBER", "PYTEST_CURRENT_TEST",
)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def local_runserver(*, debug, public_url, manage_py, argv, environ):
    """Sólo el runserver del proyecto y bindings inequívocamente locales.

    Opciones desconocidas se excluyen en vez de adivinar qué dirección sirven.
    La presencia de un marcador CI/Vercel excluye incluso valores como 'false'.
    """
    if not debug or any(key in environ for key in ("VERCEL", *CI_MARKERS)):
        return False
    try:
        if not _loopback(urlsplit(public_url).hostname):
            return False
        if len(argv) < 2 or argv[1] != "runserver" or Path(argv[0]).resolve() != Path(manage_py).resolve():
            return False
    except (ValueError, OSError):
        return False
    flags = {
        "--noreload", "--nothreading", "--ipv6", "-6", "--nostatic", "--insecure",
        "--no-color", "--force-color", "--traceback", "--skip-checks",
    }
    bindings = []
    for argument in argv[2:]:
        if argument in flags:
            continue
        if argument.startswith("-"):
            return False
        bindings.append(argument)
    if not bindings:
        return True  # Django usa 127.0.0.1 o ::1 con --ipv6.
    if len(bindings) != 1:
        return False
    address = bindings[0]
    if address.isdecimal():
        return 1 <= int(address) <= 65535
    host, separator, port = address.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return bool(separator and port.isdecimal() and 1 <= int(port) <= 65535 and _loopback(host))


def configure_pool_experiment(database, *, enabled=False, local=False):
    if not enabled:
        return database
    if not local:
        raise ImproperlyConfigured(
            "PLANB_PG_POOL_EXPERIMENT sólo está permitido en desarrollo local "
            "con runserver en loopback, DEBUG habilitado y fuera de Vercel/CI."
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
