# Experimento local de pooling PostgreSQL (etapa 4B)

Estado: opt-in, desactivado por defecto. No es una configuración de producción.
La rama parte de `main` en `7762bfe`, con las optimizaciones HTTP anteriores integradas.

## Alcance y compatibilidad

Django 6.1.1 y psycopg 3.3.6 soportan el pool nativo; se incorpora únicamente
`psycopg-pool==3.3.3`. Se verificaron los argumentos contra la firma instalada de
`ConnectionPool`. No se crea un pool alternativo ni se comparten clientes Auth.

Sin `PLANB_PG_POOL_EXPERIMENT=true`, la configuración anterior permanece igual:
`CONN_MAX_AGE=0`, sin `OPTIONS.pool` y sin health checks añadidos. No se importa
`psycopg_pool` desde la configuración ni se abre una conexión al importar settings.

La activación exige además `DJANGO_DEBUG=True` y ausencia de `VERCEL`.
Es una protección contra activación accidental; no sustituye el control del entorno.
No habilitar DEBUG en un servidor expuesto para probar este experimento.

Se conservan las credenciales, host, puerto, `sslmode` y el argumento
`options=-c search_path=django_internal` existentes. No se cambia el modo de Supavisor.
El modo sesión actual es compatible con reutilización física. Su compatibilidad real
y los límites de la cuenta no se han vuelto a consultar remotamente.

La configuración TLS mantiene `DB_SSLMODE` y su fallback anterior `prefer`.
Este experimento no refuerza ni debilita esa política. El SSLContext de HTTPX no
interviene en las conexiones psycopg/libpq.

## Configuración experimental

| Parámetro | Valor | Propósito |
| --- | --- | --- |
| `CONN_MAX_AGE` | 0 | Devolver el préstamo al finalizar cada request. |
| `CONN_HEALTH_CHECKS` | True | Django instala `ConnectionPool.check_connection`. |
| `min_size` | 0 | No precargar conexiones sin demanda. |
| `max_size` | 2 | Acotar conexiones por alias y proceso. |
| `timeout` | 3 s | Espera máxima de una adquisición. |
| `max_waiting` | 4 | Cola acotada; exceso produce `TooManyRequests`. |
| `max_idle` | 60 s | Reducir conexiones ociosas por encima del mínimo. |
| `max_lifetime` | 300 s | Reciclar conexiones, sin cortar una transacción activa. |
| `reconnect_timeout` | 5 s | Acotar cada ciclo de reconexión del worker. |

El timeout de adquisición no es un timeout de conexión TCP, consulta ni navegación.
El timeout de reconexión tampoco garantiza recuperar una transacción fallida.
No se añadieron reintentos de operaciones de negocio ni conversión de errores de Auth.

Django construye el pool perezosamente por alias y proceso. Cada solicitud toma
una conexión; al cerrarla, Django la devuelve al pool. Una conexión física no se
presta simultáneamente a dos solicitudes. Puede quedar prestada durante esperas
HTTP hasta finalizar el request. El presupuesto global depende de procesos e
instancias: no interpretar `max_size=2` como un límite global de Supabase.

Los health checks tienen costo de red. En la versión instalada el chequeo de
psycopg ejecuta una operación vacía sobre la conexión, no la consulta de negocio.
Una conexión puede romperse después del chequeo. No se garantiza el éxito de
la solicitud en curso ni se reintenta refresh/logout ante un resultado incierto.
Los errores propios del pool conservan el manejo de errores existente; el
experimento no agrega una respuesta 503 especial ni cambia autenticación.

## Activar y desactivar sin editar .env

En una terminal PowerShell local, desde la raíz:

```powershell
$env:DJANGO_DEBUG = 'True'
$env:PLANB_PG_POOL_EXPERIMENT = 'true'
.\.venv\Scripts\python.exe -B backend/manage.py runserver 127.0.0.1:8000
```

Detener el servidor y, en esa misma terminal, desactivar y reiniciar:

```powershell
Remove-Item Env:PLANB_PG_POOL_EXPERIMENT -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -B backend/manage.py runserver 127.0.0.1:8000
```

Las variables pertenecen al proceso/terminal; no se escribe `.env`. Restaurar
también el valor anterior de `DJANGO_DEBUG` al terminar si se cambió para probar.
El reinicio libera el pool del proceso anterior. Quitar la variable sin reiniciar
no cambia los settings de un servidor ya iniciado.

`runserver` crea hilos nuevos y cierra los wrappers al terminar su manejo. El
pool nativo puede conservar las conexiones físicas entre esos hilos. Aumentar
`CONN_MAX_AGE` no proporciona esa misma reutilización.
En WSGI estable hay un pool por proceso; con ASGI las operaciones ORM síncronas
siguen ejecutándose en hilos y deben conservarse `CONN_MAX_AGE=0` y el ciclo de cierre.
Este cambio no convierte las vistas en async ni instala servidores adicionales.

Vercel queda excluido de la activación. Sus instancias pueden reutilizarse y
Fluid Compute puede ejecutar solicitudes concurrentes; no debe asumirse un
proceso nuevo por petición ni una instancia estable entre navegaciones. No se
modificaron su configuración ni su documentación de despliegue en esta etapa.

## Validación offline

Desde `backend/`, usando la .venv:

```powershell
..\.venv\Scripts\python.exe -B manage.py test apps.users.tests.test_postgresql_pool apps.users.tests.test_sessions apps.users.tests.test_auth_flow apps.users.tests.test_security apps.users.tests.test_request_connections
..\.venv\Scripts\python.exe -B manage.py test
..\.venv\Scripts\python.exe -B manage.py check --settings=config.settings_test
git diff --check
```

La suite habitual utiliza SQLite en memoria, fixtures y sockets/DNS bloqueados.
Los tests nuevos verifican activación, rechazo fuera de desarrollo, parámetros,
preservación de endpoint/TLS/esquema, creación perezosa y contratos de préstamo y
devolución con el pool nativo. Los préstamos de ese contrato se simulan: no son
una medición PostgreSQL ni una prueba de locks. La configuración de ejecución
se evalúa con dotenv simulado y credenciales ficticias.

## Laboratorio PostgreSQL aislado

No forma parte del descubrimiento `test_*.py`. Requiere que el operador disponga
de PostgreSQL local y una base **vacía y desechable** llamada
`planb_pool_test_<identificador>`. Nunca apuntar al proyecto compartido Supabase.
No se instala PostgreSQL, inicia Docker ni crea una base automáticamente.

El laboratorio fija tanto `host` como `hostaddr` a `127.0.0.1`; no acepta DSN ni
host remoto. Rechaza nombres ajenos al prefijo y bases con tablas de usuario o un
esquema `django_internal` preexistente. No carga `.env` ni settings de ejecución.
Auth y perfiles se simulan; los JWT son strings ficticios.

Desde `backend/`, con credenciales exclusivas de esa base:

```powershell
$env:PLANB_POOL_TEST_PASSWORD = '<contraseña del PostgreSQL desechable>'
..\.venv\Scripts\python.exe -B -m apps.users.tests.postgresql_pool_lab --database planb_pool_test_local --user postgres --port 5432 --mode compare --samples 21 --output ../docs/testing/postgresql-pooling-results.json
Remove-Item Env:PLANB_POOL_TEST_PASSWORD
```

El laboratorio crea fixtures ORM de `Session` y `SupabaseSession` dentro de su
propio `django_internal`, sin ejecutar migraciones ni crear `django_migrations`.
Al finalizar cierra los préstamos y pools y elimina únicamente el esquema que
creó. Si el proceso se interrumpe abruptamente puede quedar ese esquema: la
siguiente ejecución lo rechaza y exige una base vacía de nuevo.

Sólo el laboratorio loopback usa `sslmode=disable` y timeouts de consulta/lock de
5/3 segundos para un PostgreSQL de pruebas sin TLS. No es una modificación del
TLS de la aplicación ni prueba la negociación TLS contra Supabase.

Pruebas preparadas para ambas variantes:

- Dos usuarios distintos y dos requests del mismo usuario, con conexiones de hilos independientes.
- Rotación concurrente del par de tokens, exactamente una llamada ficticia de refresh.
- Refresh/logout en ambos órdenes; una conexión de control local observa la espera real en `pg_stat_activity` antes de liberar la barrera.
- Sesión vencida y refresh fallido, conservando la invalidación existente.
- `search_path`, `current_schema()`, rollback y transacciones `IDLE` tras devolución.
- Interrupción de conexiones ociosas y en uso; fallo de la operación actual y recuperación en la siguiente.
- Saturación, `PoolTimeout`, límite de cola y `TooManyRequests` (sólo variante pool).
- Devolución al pool, autocommit y cierre físico del pool (sólo variante pool).

No se valida la revocación real de JWT en Supabase ni se promete resolver el
fallo distribuido «Auth renovó, pero falló el commit local». Tampoco se garantiza
que un logout pueda borrar su sesión si PostgreSQL está caído. Esos límites
preexistentes no se alteran y no justifican introducir reintentos automáticos.

## Comparación y métricas

`--mode compare` ejecuta baseline/pool/pool/baseline en procesos separados y
aborta si una variante falla. Cada ejecución corre primero la matriz anterior.
Luego reinicia el pool para medir una primera solicitud y solicitudes posteriores.

La carga es una petición WSGI en proceso con middleware Django, sesión real en
PostgreSQL y tres llamadas al servicio de token. Auth/perfil son ficticios y
constantes. La respuesta se consume y cierra para emitir `request_finished`.
No mide red HTTP del navegador ni el rendimiento de una navegación autenticada real.

| Métrica JSON | Interpretación |
| --- | --- |
| `physical_opens`, `physical_open_ms`, `connect_errors` | Conexiones psycopg realmente abiertas, tiempo de intentos y errores. No usa `connection_created`. |
| `module_opens`, `class_opens` | Aperturas observadas por `psycopg.connect` (baseline) y `Connection.connect` (pool). Se interceptan ambas rutas sin contar dos veces la misma llamada. |
| `loans`, `acquire_ms`, `acquire_errors` | Préstamos exitosos, tiempo total de intentos de adquisición y errores. |
| `reused_loans`, `backend_pids`, `connection_released` | Reutilización observada por identidad del objeto físico, PID leído de libpq durante SQL y liberación del wrapper al cerrar la respuesta. |
| `pool_wait_ms` | Delta de `requests_wait_ms` del pool; resolución de milisegundos. |
| `health_checks`, `health_ms` | Cantidad y costo del chequeo nativo. |
| `session_sql_ms`, `session_sql_count` | Operaciones SQL de la petición sobre fixtures de sesión; excluye commits del driver. |
| `active_token_ms` | Tiempo agregado dentro de `_active_token`, incluidos los costos que ocurran en ese intervalo. |
| `commits`, `commit_ms` | Llamadas a commit y su tiempo. |
| `response_ms`, `errors`, `error_type` | Respuesta WSGI completa y fallos, sin mensajes de error ni credenciales. |

Los intervalos se superponen: adquisición incluye chequeo; servicio de token y
respuesta incluyen SQL/commit. **No sumarlos como costos independientes.**
Las métricas de aperturas y chequeos pueden ejecutarse en workers; se registran
con un lock. No se registran SQL parametrizado, cookies ni tokens.

El JSON separa `first`/`later`, mediana y p95 de respuesta para las posteriores.
Una reducción de aperturas no es por sí sola una mejora de latencia. Comparar
errores, espera y consumo global. Los contadores pueden variar si el pool recicla.

La ejecución real en Docker figura más abajo. Sigue pendiente comparar servidores
reales `runserver`, WSGI y ASGI bajo igual carga, procesos y región. El fixture WSGI
no demuestra el ciclo de todos esos servidores. No hay mediciones nuevas
autenticadas reales ni estimaciones presentadas como resultados.

## Criterio para conservar el experimento

Conservar desactivado por defecto y habilitar sólo para experimentos locales
explícitos. Antes de recomendar adopción en un despliegue se exige:
menos aperturas físicas posteriores, menor mediana/p95, cero regresiones de
aislamiento/concurrencia y capacidad suficiente bajo saturación. No trasladar
los límites de laboratorio a producción sin medir procesos e instancias.

## Validación inicial, antes de disponer de Docker

- 61 tests específicos de pooling, sesiones, autenticación, seguridad y transporte: OK.
- Suite completa ejecutada desde `backend/`: 916 tests, OK, sin omitidos reportados.
- `python manage.py check`, check con `config.settings_test` y `pip check`: OK.
- Configuración efectiva inspeccionada sin conectar: pool desactivado,
  health checks desactivados, `CONN_MAX_AGE=0`, puerto `5432`, TLS `require`,
  `options=-c search_path=django_internal`.
- PostgreSQL de integración y benchmark: no ejecutados. No se encontró PostgreSQL
  local y el motor Docker no estaba disponible. No se inició ni instaló infraestructura.
- No hay un antes/después numérico nuevo. Los 270–320 ms de apertura provienen de
  la auditoría anterior y no constituyen un ahorro medido de esta implementación.
- Sin cambios en servicios de sesiones/Auth, `.env`, Supabase o Vercel; sin commit ni push.

## Corrección del laboratorio y comparación real en Docker

Se reprodujo el baseline original: ejecutaba cuatro operaciones SQL y tres
commits por respuesta, pero publicaba cero aperturas. No era una conexión ya
abierta antes de medir: Django conectaba mediante `self.Database.connect`, el
alias de módulo `psycopg.connect` enlazado al método original. Reemplazar sólo
`Connection.connect` no interceptaba ese alias. Ahora se interceptan ambas rutas
y se verifica que cada respuesta baseline abra/cierre una conexión y que cada
respuesta pool obtenga/devuelva un préstamo. La primera muestra debe observar
una apertura real. Los PIDs se capturan durante SQL, antes del cierre físico.

El fallo de saturación también era del laboratorio. En psycopg-pool 3.3.3 el
waiter que sufrió `PoolTimeout` conserva su entrada en la cola hasta que se
devuelve una conexión. El contador `requests_waiting=4` podía representar un
waiter vencido y sólo tres activos. El cuarto worker recibía legítimamente
`TooManyRequests`, que luego propagaba `future.result()`. No era una regresión
del pooling. El test ahora devuelve/reobtiene una conexión tras comprobar el
timeout, verifica que la cola quede vacía y recién entonces encola cuatro
workers. Un quinto intento verifica explícitamente `TooManyRequests`. Los cuatro
admitidos deben completar, devolver sus conexiones en `finally` y dejar dos
conexiones disponibles, sin waiters ni transacciones abiertas. Después se prueba
otra consulta para verificar recuperación. No se alteraron límites ni driver.

Ejecución: `baseline/pool/pool/baseline`, 21 solicitudes por corrida, PostgreSQL
16.16 en `planb-pg-lab`, loopback `127.0.0.1:55432`, base desechable
`planb_pool_test_local`. Django 6.1.1, psycopg 3.3.6, psycopg-pool 3.3.3.
Los parámetros SQL, fixtures, middleware y número de operaciones son iguales;
las diferencias deliberadas son el pool y sus health checks. Se mantienen
`max_size=2` y `max_waiting=4`. TLS desactivado sólo para este PostgreSQL loopback.

Resultados completos y muestras crudas: [postgresql-pooling-results.json](postgresql-pooling-results.json).
El archivo incluye parámetros, versiones, contadores por muestra, PIDs, resultados
de integración y `cleanup_verified` para cada proceso. Se publica sólo al terminar
las cuatro corridas correctamente. Si una ejecución falla, un archivo anterior
no se actualiza: siempre comprobar también el código de salida.

| Verificación | Baseline, cada corrida | Pool, cada corrida |
| --- | --- | --- |
| Integración | 7 aprobados, 2 omitidos exclusivos del pool | 9 aprobados, 0 omitidos |
| Fallos / errores de integración | 0 / 0 | 0 / 0 |
| Respuestas medidas | 21 | 21 |
| Liberación de conexiones y eliminación del esquema | Verificadas | Verificadas |

Se agregó una excepción intencional con conexión real para demostrar limpieza
también en el camino de fallo. La finalización del runner verifica que no haya
otras conexiones a su base antes de eliminar su esquema. Se verificó además
desde Docker que no quedaran esquema ni conexiones después de comparar.
Se forzó también un fallo real de la suite del runner mediante inyección de un
test fallido: devolvió código 1 y una conexión de control posterior comprobó
`schema=0` y `peers=0`. Ese fallo intencional no es una regresión ni integra las
muestras del benchmark.
Los contratos offline de instrumentación cubren el alias de módulo, errores de
apertura y reutilización por identidad; suite completa: 918 tests, OK.

Agregados por variante: 42 solicitudes, incluidas dos primeras; las medianas y
p95 siguientes usan únicamente las 40 posteriores, con percentil por rango más
cercano. No se promedian los percentiles de cada corrida.

| Métrica | Baseline | Pool |
| --- | ---: | ---: |
| Respuesta posterior, mediana | 48.127 ms | 17.640 ms |
| Respuesta posterior, p95 | 70.364 ms | 20.732 ms |
| Aperturas físicas, total | 42 | 2 |
| Préstamos / reutilizaciones observadas | 0 / 0 | 42 / 40 |
| Apertura posterior, mediana | 26.004 ms | 0 ms (sin nuevas aperturas) |
| Adquisición posterior, mediana / p95 | No aplica | 1.235 / 1.619 ms |
| Health checks, cantidad total | 0 | 42 |
| Health check posterior, mediana / p95 | No aplica | 1.218 / 1.597 ms |
| Espera de pool posterior, mediana / p95 | No aplica | 0 / 0 ms |
| Espera de pool, acumulada | No aplica | 64 ms, en las primeras solicitudes |
| SQL de sesión posterior, mediana | 14.576 ms | 9.187 ms |
| Commits, total / mediana posterior | 126 / 3.717 ms | 126 / 3.417 ms |
| Errores de respuesta / conexión / adquisición | 0 / 0 / 0 | 0 / 0 / 0 |

Las primeras respuestas se separan: baseline 48.909 y 50.653 ms; pool 63.722 y
59.798 ms. Las dos primeras del pool abrieron una conexión cada una. La espera
inicial corresponde a obtener esa conexión, no a saturación del benchmark
secuencial. Los rechazos y timeouts intencionales pertenecen al test de estrés,
no al contador de errores de las solicitudes del benchmark.

En esta carga local, la mediana posterior baja aproximadamente 30.49 ms (63.35%).
La mejora no se traslada como porcentaje a navegaciones completas ni a Supabase.
Auth es ficticio y no hay TLS ni red remota. La variación de SQL/commit no prueba
un cambio del motor de sesiones: sus cuatro operaciones SQL y tres commits por
respuesta se conservaron. Las duraciones superpuestas no deben sumarse.

Recomendación: conservar el experimento desactivado por defecto; se demostró
reutilización física y beneficio para solicitudes posteriores de este fixture.
No recomendar aún despliegue: faltan servidores reales, latencia/TLS remotos,
presupuesto de conexiones por instancias y pruebas de carga representativa.

Referencias: [Django](https://docs.djangoproject.com/en/6.1/ref/databases/#postgresql-pool),
[psycopg_pool](https://www.psycopg.org/psycopg3/docs/api/pool.html),
[Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres),
[Vercel Fluid Compute](https://vercel.com/docs/fluid-compute).
