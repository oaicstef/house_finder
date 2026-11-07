# Real Estate Search Agent

![Build and Deploy](https://github.com/oaicstef/house_finder/actions/workflows/release.yml/badge.svg)
![Test Coverage](https://github.com/oaicstef/house_finder/actions/workflows/coverage.yml/badge.svg)
![Security Scan](https://github.com/oaicstef/house_finder/actions/workflows/security.yml/badge.svg)

Backend de un **agente de búsqueda inmobiliaria personalizada**: scrapea Idealista y
Fotocasa mediante actores de Apify, clasifica los anuncios con un LLM (DeepSeek vía SDK
de OpenAI) y devuelve solo las viviendas que encajan con los criterios del usuario.
Incluye una búsqueda automática diaria a las 07:00 (APScheduler) con idempotencia por
fecha + criterio, una **UI web con fotos** y despliegue en el **N100 con Docker Compose**
(los manifiestos de Kubernetes ARM64 se mantienen como alternativa).

Construido con Clean Architecture (dominio → aplicación → infraestructura/API) y SOLID.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | Guía de uso: tokens, actores de Apify, recetas `curl`, cliente Python, errores y resolución de problemas. |
| [specs/real-estate-search-agent/plan.md](specs/real-estate-search-agent/plan.md) | Especificación técnica: ports, modelo de datos, flujo del job, excepciones y riesgos. |
| [specs/search-geo-year-filters/plan.md](specs/search-geo-year-filters/plan.md) | Radio por código postal y año de construcción. |
| [specs/search-profile-ui-k8s/plan.md](specs/search-profile-ui-k8s/plan.md) | Perfil de búsqueda configurable, fotos, UI y despliegue ARM64. |
| [specs/github-deployment-pipeline/plan.md](specs/github-deployment-pipeline/plan.md) | Diseño del pipeline de CI/CD en GitHub Actions. |
| [deploy/n100/README.md](deploy/n100/README.md) | Destino del pipeline: Compose, secretos SSH, volumen y operación en el N100. |
| [deploy/k8s/README.md](deploy/k8s/README.md) | Alternativa Kubernetes: build ARM, Secret, `kubectl apply -k` y port-forward. |
| `/docs` y `/redoc` (en ejecución) | OpenAPI interactivo autogenerado. |

## Cómo funciona

```
POST /api/v1/searches
   │
   ├─ 1. Apify        → scrapea Idealista y Fotocasa, devuelve anuncios crudos en JSON
      ├─ 2. Filtro duro  → descarta por precio / m² / habitaciones y por `must_have`
   ├─ 3. DeepSeek     → puntúa cada anuncio: relevance 0-1 + reason
   └─ 4. Repositorio  → guarda el resultado y responde 201
```

**Apify** es la pieza de scraping: Idealista y Fotocasa no tienen API pública y bloquean el
scraping directo, así que el proyecto delega en actores de Apify, que devuelven los anuncios
como JSON. Apify **solo obtiene datos crudos**; el filtrado fino y la puntuación de
relevancia los hace DeepSeek. Ver
[¿Qué papel juega Apify?](docs/USAGE.md#41-qué-papel-juega-apify).

**DeepSeek** es el clasificador: recibe los anuncios ya normalizados y responde, para cada
uno, `relevance` (0.0-1.0) y `reason` en español.

## Estructura

```
app/
├── main.py                  # create_app(), lifespan, scheduler, UI estática
├── config.py                # Settings (BaseSettings) + perfil de búsqueda
├── domain/
│   ├── models.py            # entidades y value objects (stdlib puro)
│   ├── exceptions.py        # DomainException + code/http_status
│   └── ports.py             # ScraperPort, ClassifierPort, ListingRepository
├── application/services/
│   ├── search_service.py    # orquesta scraping → clasificación → persistencia
│   └── scheduled_search.py  # job diario idempotente
├── infrastructure/
│   ├── scrapers/apify_scraper.py
│   ├── classifiers/deepseek_classifier.py
│   ├── geocoding/carto_ciudad_locator.py
│   ├── repositories/          # memory (volátil) + json_file (persistente)
│   └── logging_config.py    # structlog (JSON en prod, consola en dev)
├── api/
│   ├── schemas.py           # DTOs Pydantic v2
│   ├── dependencies.py      # composition root + Depends()
│   ├── middleware.py        # request_id + duration_ms
│   ├── exception_handlers.py
│   └── routes.py            # endpoints delgados
├── web/                     # UI: index.html + styles.css + app.js (sin build)
└── tests/                   # pytest + pytest-asyncio (sin APIs reales)
deploy/n100/                 # Docker Compose del N100 (destino del pipeline)
deploy/k8s/                  # Alternativa: Namespace, ConfigMap, PVC, Deployment ARM64, Service
Dockerfile                   # imagen multi-arch (python:3.13-slim)
specs/                       # planes por feature (skill plan-first)
```

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements-dev.txt
cp .env.example .env            # rellena APIFY_TOKEN y DEEPSEEK_API_KEY
```

## Ejecución

```bash
uvicorn app.main:app --reload
```

- **UI con fotos**: <http://127.0.0.1:8000/> (botón «Ejecutar búsqueda»)
- Documentación interactiva: <http://127.0.0.1:8000/docs>
- Health check: `GET /health`

Si faltan `APIFY_TOKEN` o `DEEPSEEK_API_KEY`, el arranque falla con
`ConfigurationException` (`CONFIGURATION_ERROR`), nunca con un `KeyError`.

## Perfil de búsqueda configurable

El cuerpo de la búsqueda se define una sola vez en `SEARCH_PROFILE_JSON` y lo comparten el
job diario, la UI y `POST /api/v1/search-profile/run`:

```dotenv
SEARCH_PROFILE_JSON={"location":"Madrid","portals":["idealista","fotocasa"],"max_price_eur":900000,"min_size_m2":140,"min_rooms":4,"max_results":50,"preferences":"Casa o chalet, padel cominitario, piscina comunitaria","must_have":["padel"],"postal_code":"28031","radius_km":5,"min_year_built":2020}
```

- `GET /api/v1/search-profile` devuelve el perfil activo.
- `POST /api/v1/search-profile/run` ejecuta exactamente ese perfil (`trigger: "profile"`).
- `SEARCH_PROFILE_JSON=` (vacío) recupera los criterios heredados `DAILY_*`.
- Un JSON inválido impide arrancar con `CONFIGURATION_ERROR` y el campo culpable en
  `details.fields`.

## CI/CD y despliegue en el N100

El repositorio se despliega solo con GitHub Actions (`.github/workflows/`):

| Workflow | Disparador | Qué hace |
|---|---|---|
| `coverage.yml` | PR y push a `main` | Instala `requirements-dev.txt`, ejecuta `pytest --cov`, publica el resumen en el job y sube `htmlcov/` + `coverage.xml`. |
| `security.yml` | PR, push a `main` y lunes 06:00 UTC | `pip-audit` sobre runtime y dev, Trivy filesystem/IaC y Trivy de la imagen; falla con HIGH/CRITICAL. |
| `release.yml` | push a `main`, tags `v*` y manual | Construye la imagen `linux/amd64`, la publica en GHCR (`latest`, `<ref>` y `sha-<commit>`), entra por SSH en el N100, hace `git pull` y levanta `deploy/n100/docker-compose.yml` con ese tag. Con un tag `v*` crea además el GitHub Release. |

`dependabot.yml` abre PRs agrupados para `pip`, GitHub Actions y la imagen base Docker.

**Secretos del repositorio** (Settings → Secrets and variables → Actions):

| Secreto | Obligatorio | Uso |
|---|---|---|
| `N100_HOST` | sí | IP o host del N100 (p. ej. `192.168.0.63`). |
| `N100_USER` | sí | Usuario SSH con acceso a Docker. |
| `N100_PASSWORD` | sí | Contraseña SSH del usuario del N100 (alternativa a la clave). |
| `N100_SSH_KEY` | alternativa | Clave privada SSH sin passphrase (si no usas contraseña). |
| `N100_PORT` | no | Puerto SSH (22 por defecto). |
| `N100_DEPLOY_PATH` | no | Checkout del repo en el N100 (`$HOME/house_finder` por defecto). |
| `APIFY_TOKEN` | sí | Token de Apify; el deploy lo escribe en el `.env` del N100. |
| `DEEPSEEK_API_KEY` | sí | Clave del clasificador; el deploy la escribe en el `.env` del N100. |
| `REGISTRY_USERNAME` | opcional | Usuario GHCR (`oaicstef`); sin él, el N100 construye la imagen desde el checkout. |
| `REGISTRY_PASSWORD` | opcional | PAT con `read:packages` para descargar la imagen ya construida. |

`GITHUB_TOKEN` es implícito: publica en GHCR y crea el Release. Cada despliegue usa el tag
`sha-<commit>`, así que el N100 ejecuta exactamente la imagen construida para ese commit.

Despliegue manual (si hace falta saltarse el pipeline):

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/oaicstef/house-finder:0.1.0 --push .
# En el N100:
cd ~/house_finder/deploy/n100
cp .env.example .env   # solo la primera vez; rellena los tokens
./deploy.sh 0.1.0
```

Detalles del N100 (Compose, secretos, volumen, operación) en
[deploy/n100/README.md](deploy/n100/README.md). El despliegue en Kubernetes ARM64 sigue
disponible como alternativa en [deploy/k8s/README.md](deploy/k8s/README.md).

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` \| `production` \| `test`. En `production` los logs son JSON. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `ENABLE_SCHEDULER` | `true` | Activa el cron diario (07:00, `TIMEZONE`). |
| `TIMEZONE` | `Europe/Madrid` | Zona horaria del cron. |
| `APIFY_TOKEN` | — | **Obligatoria**. Token de Apify. |
| `APIFY_IDEALISTA_ACTOR` | `idealista-scraper` | Actor de Apify para Idealista. |
| `APIFY_FOTOCASA_ACTOR` | `fotocasa-scraper` | Actor de Apify para Fotocasa. |
| `SEARCH_OPERATION` | `rent` | `rent` (alquiler) \| `sale` (compraventa). |
| `APIFY_MAX_ITEMS` | `40` | Tope de anuncios solicitados por portal. |
| `APIFY_TIMEOUT_SECONDS` | `300` | Timeout de la ejecución del actor. |
| `APIFY_MAX_CHARGE_USD` | sin límite | Tope de gasto por ejecución de actor, en USD. |
| `GEOCODING_BASE_URL` | `https://www.cartociudad.es/geocoder/api/geocoder` | Geocodificador para resolver `postal_code`. |
| `GEOCODING_TIMEOUT_SECONDS` | `10` | Timeout de la consulta de geocodificación. |
| `DEEPSEEK_API_KEY` | — | **Obligatoria**. Clave del LLM. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Endpoint compatible con OpenAI. |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Modelo usado en la clasificación. |
| `SEARCH_PROFILE_JSON` | cuerpo con CP 28031 + 5 km + año 2020 | Perfil único (job diario + UI). Vacío = usa `DAILY_*`. |
| `DAILY_SEARCH_*` | Madrid / 1500 € / 60 m² / 2 hab. | Criterios del job diario (solo si no hay perfil). |
| `DAILY_MUST_HAVE` | vacío (sin filtro) | Requisitos obligatorios del job diario, separados por comas (`piscina,padel`). |
| `DAILY_POSTAL_CODE` | vacío | CP del job diario; requiere `DAILY_RADIUS_KM`. |
| `DAILY_RADIUS_KM` | vacío | Radio (1-50 km) alrededor del CP del job diario. |
| `DAILY_MIN_YEAR_BUILT` | vacío | Año mínimo de construcción del job diario. |
| `RELEVANCE_THRESHOLD` | `0.6` | Umbral mínimo de relevancia para aceptar un anuncio. |
| `RESULTS_DIR` | `data/results` | Directorio de resultados JSON (vacío = solo memoria). |

## API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | UI con fotos de los anuncios (botón «Ejecutar búsqueda»). |
| `POST` | `/api/v1/searches` | Búsqueda síncrona. `201` + anuncios filtrados. `502` scraper, `503` clasificador, `422` validación. |
| `GET` | `/api/v1/search-profile` | Perfil de búsqueda activo (`SEARCH_PROFILE_JSON`). |
| `POST` | `/api/v1/search-profile/run` | Ejecuta el perfil configurado (`trigger: profile`). |
| `GET` | `/api/v1/searches/{search_id}/results` | Resultados de una búsqueda previa. `404` `SEARCH_NOT_FOUND`. |
| `POST` | `/api/v1/scheduled-searches/run` | Dispara el job diario. `executed=false` si ya se ejecutó hoy. |
| `GET` | `/health` | Estado y si el scheduler está activo. |

Ejemplo:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/searches \
  -H "Content-Type: application/json" \
  -d '{"location":"Madrid","max_price_eur":1500,"min_size_m2":70,"min_rooms":2,
       "must_have":["piscina"],
       "preferences":"luminoso, con ascensor, cerca del metro"}'
```

- `max_results` recorta la **respuesta**, no la búsqueda: el scraper pide hasta
  `APIFY_MAX_ITEMS` candidatos.
- `must_have` es **obligatorio** (AND); `preferences` solo suma relevancia.
- `postal_code` + `radius_km` busca en un radio alrededor del CP (CartoCiudad) y
  `min_year_built` exige un año de construcción mínimo.

Errores uniformes:

```json
{"error": {"code": "SCRAPER_FAILURE", "message": "…", "request_id": "…"}}
```

## Logging

`structlog` con `JSONRenderer` en producción y `ConsoleRenderer` en desarrollo.
Un middleware genera `request_id` (UUID4 o el `X-Request-ID` entrante), lo propaga por
`contextvars` y devuelve la cabecera `X-Request-ID`. Todos los eventos llevan
`event`, `request_id`, `search_id`, `listing_url`, `duration_ms` y `error_code`.

## Job diario

`AsyncIOScheduler` + `CronTrigger(hour=7, minute=0)`, arrancado en el `lifespan`.
Idempotente: si ya existe un resultado para el mismo `criteria_fingerprint` y fecha,
emite `scheduled_search_skipped_duplicate` (WARNING) y termina sin error. Todo el job
está envuelto en `try/except` porque una excepción no capturada en una tarea programada
se pierde. Desactivable con `ENABLE_SCHEDULER=false`.

## Tests

```bash
pytest
```

Sin llamadas reales a Apify, DeepSeek ni CartoCiudad: los puertos se sustituyen por fakes
(`app/tests/conftest.py`). Cubre servicio de búsqueda, job diario, normalización del
scraper (incluidas fotos), parseo del clasificador, perfil configurable, UI estática y
endpoints.

## Decisiones destacadas

- **DIP**: los servicios dependen de `app/domain/ports.py`; el único punto que conoce
  las implementaciones es `build_container()` en `api/dependencies.py`.
- **OCP**: añadir un portal es crear otro actor + entrada en `actor_ids`; añadir otro LLM
  es escribir un nuevo `ClassifierPort`. Nada más cambia.
- **Persistencia**: `JsonFileListingRepository` guarda un JSON por búsqueda en
  `data/results/` (incluye los **enlaces** de cada anuncio) y reconstruye su índice al
  arrancar, así que los resultados y la idempotencia diaria sobreviven a un reinicio.
  Con `RESULTS_DIR=` vacío se usa `InMemoryListingRepository` (solo memoria). Cambiar a
  Postgres/Redis es escribir otro adaptador del mismo port, sin tocar servicios.

Documentación de la feature: [specs/real-estate-search-agent/plan.md](specs/real-estate-search-agent/plan.md).
Guía de uso paso a paso: [docs/USAGE.md](docs/USAGE.md).
