# Desplegar la webapp en Vercel

## Qué causaba el 500

`FUNCTION_INVOCATION_FAILED` significa que la función serverless arrancó y
murió antes de responder. En este proyecto hay dos motivos:

1. `backend/config/settings.py` lee con `os.environ[...]` (obligatorio) ocho
   variables: `DJANGO_SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`,
   `SUPABASE_SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` y `DB_HOST`.
   Sin ellas el import de settings lanza `KeyError` y **toda** petición devuelve
   500.
2. No existía ningún punto de entrada que conectara Vercel con el WSGI de
   Django. Ahora está en `api/index.py` y enruta `vercel.json`.

Además, arrastrar la carpeta del proyecto a Vercel sube `.env`, `.venv` y
`node_modules` tal cual. Si `.env` quedó público en
`https://<tu-dominio>/.env`, **rotá ya** `SUPABASE_SECRET_KEY` y la contraseña
del rol de base de datos. El `.vercelignore` de este repo evita que vuelva a
pasar.

## Cómo compila Vercel

Vercel detecta `backend/manage.py` e importa `config.settings` para leer
`WSGI_APPLICATION` (`config.wsgi.application`), pero el runtime publica la app
como una única Función usando el entrypoint ASGI que encuentra al lado
(`backend/config/asgi.py`). También corre `collectstatic` por su cuenta y sirve
`/static/` desde el CDN, así que no hace falta entrypoint propio ni `rewrites`.

Como el runtime importa `asgi.py` por ruta absoluta y sin agregar `backend/` al
`sys.path`, `asgi.py` y `wsgi.py` insertan su carpeta padre en `sys.path` antes
de `get_*_application()`. Sin ese ajuste el runtime falla con
`ModuleNotFoundError: No module named 'config'` aunque el build pase. El
`vercel.json` sólo fija `maxDuration` sobre `backend/config/asgi.py`.

Consecuencia importante: **el build importa `settings.py`**, así que las
variables sensibles tienen que existir antes de compilar. Si falta una, el build
corta con `DJANGO_SETTINGS_FAILED` y el `KeyError` de la variable.

## Pasos

1. En Vercel: **Add New → Project → Import Git Repository** y elegí
   `VirBruno/PlanB`. No uses arrastrar y soltar: ese flujo no instala Python ni
   ejecuta el build.
2. Cargá las variables en **Settings → Environment Variables** (Production y
   Preview):

| Variable | Valor |
| --- | --- |
| `DJANGO_SECRET_KEY` | Clave generada, distinta de la local. |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | Host sin protocolo, p. ej. `planb-abc.vercel.app`. |
| `DJANGO_PUBLIC_URL` | `https://planb-abc.vercel.app`, sin barra final. |
| `SUPABASE_URL` | URL HTTPS del proyecto Supabase. |
| `SUPABASE_PUBLISHABLE_KEY` | Clave publishable. |
| `SUPABASE_SECRET_KEY` | Clave secreta, sólo servidor. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` | Rol de ejecución y host de PostgreSQL. |
| `DB_PORT` | `5432` (o el del pooler). |
| `DB_SSLMODE` | `require` |
| `PYTHON_VERSION` | `3.12` |
| `GEOAPIFY_API_KEY`, `GEMINI_API_KEY` | Opcionales; sin ellas sólo se desactivan las recomendaciones. |

3. Deploy. Vercel instala `requirements.txt`, corre `npm ci` (para que
   `collectstatic` encuentre Leaflet en `node_modules`) y compila los estáticos.
4. Aplicá las migraciones de Django desde tu máquina con el rol migrador
   (`python manage.py migrate`), apuntando al PostgreSQL de producción. La tabla
   de sesiones vive en `django_internal` y debe existir antes del primer login.

## Límites de Vercel que conviene tener presentes

- Cada request corre en una función nueva: no hay proceso persistente ni
  scheduler. `python manage.py generate_plan_reminders` debe ejecutarse desde
  un cron externo; Vercel Cron sólo hace GET a una URL.
- `maxDuration` queda en 60 s. Las llamadas a Gemini/Geoapify pueden superarlo
  y devolver 504.
- Las conexiones a PostgreSQL se abren por request; conviene usar el pooler de
  Supabase en **modo sesión** (`DB_PORT=5432`, usuario
  `planb_django_runtime.<ref>`) porque las opciones de conexión fijan
  `search_path=django_internal`, y el modo transacción (`6543`) no lo conserva.
  Mantené `DB_SSLMODE` en `require`.
- Si preferís un proceso tradicional (sin cold starts ni límites de duración),
  Render o Fly.io corren este mismo repo con `gunicorn config.wsgi` y las mismas
  variables, sin cambios de código.
