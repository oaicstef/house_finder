# Real Estate Search Agent

![Build and Deploy](https://github.com/oaicstef/house_finder/actions/workflows/release.yml/badge.svg)
![Test Coverage](https://github.com/oaicstef/house_finder/actions/workflows/coverage.yml/badge.svg)
![Security Scan](https://github.com/oaicstef/house_finder/actions/workflows/security.yml/badge.svg)

Backend de un **agente de búsqueda inmobiliaria personalizada**: scrapea Idealista y
Fotocasa mediante actores de Apify, clasifica los anuncios con un LLM (DeepSeek vía SDK
de OpenAI) y devuelve solo las viviendas que encajan con los criterios del usuario.
Incluye una búsqueda automática diaria a las 07:00 (APScheduler) con idempotencia por
fecha + criterio, **una notificación push al móvil (iPhone y Android) por cada vivienda
nueva** respecto a la ejecución anterior, una **UI web con fotos** y despliegue en
**Kubernetes ARM64**
(el despliegue en el N100 con Docker Compose se mantiene como alternativa).

Construido con Clean Architecture (dominio → aplicación → infraestructura/API) y SOLID.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | Guía de uso: tokens, actores de Apify, recetas `curl`, cliente Python, errores y resolución de problemas. |
| [specs/real-estate-search-agent/plan.md](specs/real-estate-search-agent/plan.md) | Especificación técnica: ports, modelo de datos, flujo del job, excepciones y riesgos. |
| [specs/search-geo-year-filters/plan.md](specs/search-geo-year-filters/plan.md) | Radio por código postal y año de construcción. |
| [specs/search-profile-ui-k8s/plan.md](specs/search-profile-ui-k8s/plan.md) | Perfil de búsqueda configurable, fotos, UI y despliegue ARM64. |
| [specs/github-deployment-pipeline/plan.md](specs/github-deployment-pipeline/plan.md) | Diseño del pipeline de CI/CD en GitHub Actions. |
| [specs/ui-editable-criteria/plan.md](specs/ui-editable-criteria/plan.md) | Criterios editables desde la UI y última búsqueda siempre visible. |
| [deploy/k8s/README.md](deploy/k8s/README.md) | Destino del pipeline: build ARM, Secret, `kubectl apply -k`, Ingress HTTPS, CI/CD y port-forward. |
| [deploy/n100/README.md](deploy/n100/README.md) | Alternativa: Docker Compose en el N100 (SSH, volumen y operación). |
| `/docs` y `/redoc` (en ejecución) | OpenAPI interactivo autogenerado. |

## Cómo funciona

```
POST /api/v1/searches
   │
   ├─ 1. Scraper      → Idealista y Fotocasa, un proveedor por portal (HTTP propio o Apify)
      ├─ 2. Filtro duro  → descarta por precio / m² / habitaciones y por `must_have`
   ├─ 3. DeepSeek     → puntúa cada anuncio: relevance 0-1 + reason
   └─ 4. Repositorio  → guarda el resultado y responde 201
```

El **scraper** tiene un proveedor por portal, elegido con `SCRAPER_PROVIDERS`:

| Proveedor | Cómo obtiene los anuncios | Credenciales |
| --- | --- | --- |
| `local` | HTTP directo al portal y parseo de su JSON embebido (Fotocasa) | ninguna |
| `apify` | actor de Apify (Idealista y Fotocasa) | `APIFY_TOKEN` |
| `none` | portal desactivado | — |

Por defecto **Fotocasa** usa el scraper propio y **Idealista** sigue en Apify: Idealista está
detrás de DataDome y rechaza clientes HTTP (403) incluso desde IP residencial, mientras que
Fotocasa sirve la búsqueda ya renderizada. Si la fuente local falla, la búsqueda continúa con
Apify para ese portal (`SCRAPER_FALLBACK_TO_APIFY`). El scraper **solo obtiene datos crudos**;
el filtrado fino y la puntuación los hace DeepSeek. Ver
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
│   ├── scrapers/
│   │   ├── normalization.py     # item crudo -> Listing (independiente del proveedor)
│   │   ├── filtering.py         # filtros duros (radio, precio, m², habitaciones)
│   │   ├── listing_scraper.py   # orquesta una fuente por portal + respaldo
│   │   ├── factory.py           # SCRAPER_PROVIDERS -> proveedor por portal
│   │   ├── apify_scraper.py     # adaptador Apify (proveedor opcional)
│   │   └── sources/             # http.py, fotocasa.py, apify.py, base.py
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
├── tools/scraper_probe.py   # prueba de humo del scraper por portal
├── web/                     # UI: index.html + styles.css + app.js (sin build)
└── tests/                   # pytest + pytest-asyncio (sin APIs reales)
deploy/k8s/                  # Namespace, ConfigMap, PVC, Deployment ARM64, Service
deploy/n100/                 # Alternativa: Docker Compose para el N100
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

- **UI con fotos**: <http://127.0.0.1:8000/> (criterios editables, última búsqueda
  guardada y botón «Ejecutar búsqueda»)
- Documentación interactiva: <http://127.0.0.1:8000/docs>
- Health check: `GET /health` (incluye estado y próxima ejecución del scheduler)

Si faltan `APIFY_TOKEN` o `DEEPSEEK_API_KEY`, el arranque falla con
`ConfigurationException` (`CONFIGURATION_ERROR`), nunca con un `KeyError`.

## Perfil de búsqueda configurable

El perfil se define una sola vez y lo comparten el job diario, la UI y
`POST /api/v1/search-profile/run`:

```dotenv
SEARCH_PROFILE_JSON={"location":"Madrid","portals":["idealista","fotocasa"],"max_price_eur":900000,"min_size_m2":140,"min_rooms":4,"max_results":50,"preferences":"Casa o chalet, padel cominitario, piscina comunitaria","must_have":["piscina"],"preferred_terms":["padel"],"home_types":["terracedHouse","semiDetachedHouse","detachedHouse"],"postal_code":"28031","radius_km":5,"min_year_built":2020,"max_energy_rating":"B"}
```

- **Editable desde la UI**: el formulario «Editar criterios» guarda el perfil y pasa a ser
  el que usan la búsqueda manual y el job diario, sin reiniciar el pod.
- `GET /api/v1/search-profile` devuelve el perfil activo y su `source`
  (`override` = guardado en la UI, `profile` = `SEARCH_PROFILE_JSON`, `legacy` = `DAILY_*`).
- `PUT /api/v1/search-profile` guarda el perfil editado (mismo cuerpo que `POST /searches`).
- `DELETE /api/v1/search-profile` restaura el perfil preconfigurado.
- `POST /api/v1/search-profile/run` ejecuta el perfil efectivo (`trigger: "profile"`).
- `SEARCH_PROFILE_JSON=` (vacío) recupera los criterios heredados `DAILY_*`.
- Un JSON inválido impide arrancar con `CONFIGURATION_ERROR` y el campo culpable en
  `details.fields`.

El perfil guardado vive en `search-profile.json` junto al directorio de resultados
(`/data/search-profile.json` con el PVC de Kubernetes), así que sobrevive a los reinicios y
lo ven todos los dispositivos.

## CI/CD y despliegue en Kubernetes (ARM64)

El repositorio se despliega solo con GitHub Actions (`.github/workflows/`):

| Workflow | Disparador | Qué hace |
|---|---|---|
| `coverage.yml` | PR y push a `main` | Instala `requirements-dev.txt`, ejecuta `pytest --cov`, publica el resumen en el job y sube `htmlcov/` + `coverage.xml`. |
| `security.yml` | PR, push a `main` y lunes 06:00 UTC | `pip-audit` sobre runtime y dev, Trivy filesystem/IaC y Trivy de la imagen; falla con HIGH/CRITICAL. |
| `release.yml` | push a `main`, tags `v*` y manual | Construye la imagen `linux/arm64`, la publica en GHCR (`latest`, `latest-arm64`, `<ref>` y `sha-<commit>`), aplica `deploy/k8s` y espera el rollout. Con un tag `v*` crea además el GitHub Release. |

`dependabot.yml` abre PRs agrupados para `pip`, GitHub Actions y la imagen base Docker.

**Secretos del repositorio** (Settings → Secrets and variables → Actions):

| Secreto | Uso |
|---|---|
| `KUBE_CONFIG` | Kubeconfig con permisos sobre el namespace `house-finder`. |
| `REGISTRY_USERNAME` | Usuario GHCR para el pull secret del clúster (`oaicstef`). |
| `REGISTRY_PASSWORD` | PAT con `read:packages` para ese pull secret. |

`GITHUB_TOKEN` es implícito: publica en GHCR y crea el Release. El workflow crea el
namespace y el secret `ghcr-login-secret` si no existen, y fija el Deployment al tag
`sha-<commit>` para que cada despliegue sea reproducible. Los tokens de la aplicación
(`APIFY_TOKEN`, `DEEPSEEK_API_KEY`) viven en el Secret `house-finder-secrets` del clúster,
no en GitHub.

Despliegue manual (si hace falta saltarse el pipeline):

```bash
docker buildx build --platform linux/arm64 \
  -t ghcr.io/oaicstef/house-finder:0.1.0 --push .
kubectl apply -k deploy/k8s
kubectl -n house-finder set image deployment/house-finder \
  api=ghcr.io/oaicstef/house-finder:0.1.0
kubectl -n house-finder rollout status deployment/house-finder
```

La UI queda publicada en `https://oaichousefinder.duckdns.org` (Ingress nginx + TLS de
cert-manager). Para depurar sin pasar por el Ingress:
`kubectl -n house-finder port-forward svc/house-finder 8000:80`. El registro A de DuckDNS
debe apuntar a la IP pública del clúster; el detalle está en
[deploy/k8s/README.md](deploy/k8s/README.md).

Detalles (Secret de tokens, PVC, cambio del perfil con ConfigMap, réplica única por el
scheduler, pipeline) en [deploy/k8s/README.md](deploy/k8s/README.md). La alternativa con
Docker Compose en el N100 sigue en [deploy/n100/README.md](deploy/n100/README.md).

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` \| `production` \| `test`. En `production` los logs son JSON. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `ENABLE_SCHEDULER` | `true` | Activa el cron diario (07:00, `TIMEZONE`). |
| `TIMEZONE` | `Europe/Madrid` | Zona horaria del cron. |
| `ALERTS_ENABLED` | `false` | Activa la notificación push con las viviendas nuevas del job de las 07:00. |
| `NTFY_BASE_URL` | `https://ntfy.sh` | Servidor ntfy (público o propio). |
| `NTFY_TOPIC` | vacío | Topic al que se suscribe la app del móvil. Obligatorio con `ALERTS_ENABLED=true`; llévalo al Secret, no a git (en Kubernetes lo inyecta el pipeline desde el secreto `NTFY_TOPIC` del repositorio). |
| `NTFY_TOKEN` | vacío | Token, solo si el topic está protegido en el servidor ntfy. |
| `NTFY_PRIORITY` | `3` | Prioridad de la notificación (1 mínima – 5 urgente). |
| `ALERT_CLICK_URL` | vacío | URL que abre la notificación (vacío = la primera vivienda nueva). |
| `SCRAPER_PROVIDERS` | `idealista=apify,fotocasa=local` | Mapa `portal=proveedor` (`local` / `idealista-api` / `apify` / `none`). Idealista API requiere acceso oficial aprobado. Los portales no listados quedan desactivados. |
| `SCRAPER_FALLBACK_TO_APIFY` | `true` | Si la fuente local falla o no devuelve anuncios, se reintenta con Apify para ese portal. |
| `SCRAPER_MAX_ITEMS` | `40` | Anuncios candidatos que se piden por portal. |
| `SCRAPER_MAX_PAGES` | `5` | Páginas de búsqueda que se recorren como máximo (30 anuncios por página). |
| `SCRAPER_TIMEOUT_SECONDS` | `20` | Timeout por petición HTTP del scraper propio. |
| `SCRAPER_MAX_RETRIES` | `2` | Reintentos con backoff en 429/5xx. |
| `SCRAPER_RATE_LIMIT_PER_MINUTE` | `20` | Peticiones por minuto contra cada portal. |
| `SCRAPER_USER_AGENT` | vacío (UA de navegador) | Cabecera `User-Agent` del scraper propio. |
| `SCRAPER_PROXY_URL` | vacío | Egreso alternativo (proxy residencial o relay) si el portal bloquea la IP. |
| `SCRAPER_FOTOCASA_LOCATION_SEGMENT` | vacío | Segmento de ubicación de Fotocasa cuando el texto libre no resuelve. |
| `APIFY_TOKEN` | — | **Obligatoria solo si** algún portal usa el proveedor `apify`. |
| `IDEALISTA_API_CLIENT_ID`, `IDEALISTA_API_CLIENT_SECRET` | — | Credenciales de partner, obligatorias solo con `idealista=idealista-api`; requieren aprobación de Idealista. |
| `APIFY_IDEALISTA_ACTOR` | `igolaizola/idealista-scraper` | Actor de Apify para Idealista (verificado). |
| `APIFY_FOTOCASA_ACTOR` | vacío (portal omitido) | Actor de Apify para Fotocasa; pon `igolaizola/fotocasa-scraper` para activarlo (pay-per-event). |
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
| `DAILY_MAX_ENERGY_RATING` | vacío | Certificado energético mínimo (`A` mejor - `G` peor; `B` = B o mejor). |
| `DAILY_PREFERRED_TERMS` | vacío | Términos deseables (no excluyentes) separados por comas; se destacan y se ordenan primero. |
| `DAILY_HOME_TYPES` | vacío | Tipos de vivienda separados por comas (`piso`, `adosado`, `pareado`, `independiente`…). Vacío = todos. |
| `RELEVANCE_THRESHOLD` | `0.6` | Umbral mínimo de relevancia para aceptar un anuncio. |
| `RESULTS_DIR` | `data/results` | Directorio de resultados JSON (vacío = solo memoria). |

## API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | UI: criterios editables, última búsqueda guardada y fotos de los anuncios. |
| `POST` | `/api/v1/searches` | Búsqueda síncrona. `201` + anuncios filtrados. `502` scraper, `503` clasificador, `422` validación. |
| `GET` | `/api/v1/search-profile` | Perfil efectivo y su origen (`override`/`profile`/`legacy`). |
| `PUT` | `/api/v1/search-profile` | Guarda el perfil editado en la UI; lo usan la UI y el job diario. |
| `DELETE` | `/api/v1/search-profile` | Restaura el perfil preconfigurado (`SEARCH_PROFILE_JSON` o `DAILY_*`). |
| `POST` | `/api/v1/search-profile/run` | Ejecuta el perfil efectivo (`trigger: profile`). |
| `GET` | `/api/v1/searches/latest` | Última búsqueda guardada (la que pinta la UI al abrirse). `404` si no hay. |
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
- `location`: usa el nombre del municipio (`"Madrid"`). Idealista no resuelve formas
  compuestas como `"Madrid, Madrid"` (se recorta automáticamente el sufijo de provincia).
- La respuesta incluye `portals` (`[{"portal": "idealista", "matched": 0}]`) y `warnings`:
  si un portal se queda a cero, lo verás ahí en lugar de recibir resultados parciales
  silenciosos.
- `postal_code` + `radius_km` busca en un radio alrededor del CP (CartoCiudad),
  `min_year_built` exige un año de construcción mínimo, `max_energy_rating` exige un
  certificado energético mínimo (`"B"` = A o B), `home_types` limita el subtipo de
  vivienda (p. ej. `["terracedHouse","semiDetachedHouse","detachedHouse"]`) y
  `distance_km` (en cada anuncio) permite ordenar por cercanía desde la UI.

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
