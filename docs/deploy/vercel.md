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

## Build sin secretos

`collectstatic` corre con `DJANGO_SETTINGS_MODULE=config.settings_build`, un
módulo que sólo importa `settings_base` y la configuración de estáticos. Así el
build no depende de `DJANGO_SECRET_KEY`, Supabase ni la base.

`vercel.json` fija `"framework": null` (preset "Other"). Sin ese ajuste, Vercel
detecta `backend/manage.py`, importa `config.settings` durante el build y falla
con `DJANGO_SETTINGS_FAILED` si todavía no cargaste las variables. Con "Other",
`api/index.py` se publica como función y expone `app`/`application` como WSGI.
Las variables se necesitan recién en runtime, donde sí hay que tenerlas todas.

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

3. Deploy. `vercel.json` ejecuta `npm ci` y `collectstatic`; WhiteNoise sirve
   CSS, JS y Leaflet desde la propia función. Los estáticos se incluyen en el
   bundle con `includeFiles`.
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
  Supabase (`DB_PORT` del pooler, normalmente `6543`) y mantener `DB_SSLMODE`
  en `require`.
- Si preferís un proceso tradicional (sin cold starts ni límites de duración),
  Render o Fly.io corren este mismo repo con `gunicorn config.wsgi` y las mismas
  variables, sin cambios de código.
