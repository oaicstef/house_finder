# Real Estate Search Agent

Backend de un **agente de búsqueda inmobiliaria personalizada**: scrapea Idealista y
Fotocasa mediante actores de Apify, clasifica los anuncios con un LLM (DeepSeek vía SDK
de OpenAI) y devuelve solo las viviendas que encajan con los criterios del usuario.
Incluye una búsqueda automática diaria a las 07:00 (APScheduler) con idempotencia por
fecha + criterio.

Construido con Clean Architecture (dominio → aplicación → infraestructura/API) y SOLID.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | Guía de uso: tokens, actores de Apify, recetas `curl`, cliente Python, errores y resolución de problemas. |
| [specs/real-estate-search-agent/plan.md](specs/real-estate-search-agent/plan.md) | Especificación técnica: ports, modelo de datos, flujo del job, excepciones y riesgos. |
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
├── main.py                  # create_app(), lifespan, scheduler
├── config.py                # Settings (BaseSettings) + validación al arranque
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
│   ├── repositories/          # memory (volátil) + json_file (persistente)
│   └── logging_config.py    # structlog (JSON en prod, consola en dev)
├── api/
│   ├── schemas.py           # DTOs Pydantic v2
│   ├── dependencies.py      # composition root + Depends()
│   ├── middleware.py        # request_id + duration_ms
│   ├── exception_handlers.py
│   └── routes.py            # endpoints delgados
└── tests/                   # pytest + pytest-asyncio (sin APIs reales)
specs/real-estate-search-agent/plan.md   # plan (skill plan-first)
.github/skills/plan-first/SKILL.md       # skill reutilizable
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

- Documentación interactiva: <http://127.0.0.1:8000/docs>
- Health check: `GET /health`

Si faltan `APIFY_TOKEN` o `DEEPSEEK_API_KEY`, el arranque falla con
`ConfigurationException` (`CONFIGURATION_ERROR`), nunca con un `KeyError`.

## Variables de entorno

| Variable | Por defecto | Descripción |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` \| `production` \| `test`. En `production` los logs son JSON. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `ENABLE_SCHEDULER` | `true` | Activa el cron diario (07:00, `TIMEZONE`). |
| `TIMEZONE` | `Europe/Madrid` | Zona horaria del cron. |
| `APIFY_TOKEN` | — | **Obligatoria**. Token de Apify. |
| `APIFY_IDEALISTA_ACTOR` | `igolaizola/idealista-scraper` | Actor de Apify para Idealista (verificado). |
| `APIFY_FOTOCASA_ACTOR` | vacío (portal omitido) | Actor de Apify para Fotocasa; pon `igolaizola/fotocasa-scraper` para activarlo (pay-per-event). |
| `SEARCH_OPERATION` | `rent` | `rent` (alquiler) \| `sale` (compraventa). |
| `APIFY_MAX_ITEMS` | `40` | Tope de anuncios solicitados por portal. |
| `APIFY_TIMEOUT_SECONDS` | `300` | Timeout de la ejecución del actor. |
| `APIFY_MAX_CHARGE_USD` | sin límite | Tope de gasto por ejecución de actor, en USD. |
| `DEEPSEEK_API_KEY` | — | **Obligatoria**. Clave del LLM. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | Endpoint compatible con OpenAI. |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Modelo usado en la clasificación. |
| `DAILY_SEARCH_*` | Madrid / 1500 € / 60 m² / 2 hab. | Criterios del job diario. |
| `DAILY_MUST_HAVE` | vacío (sin filtro) | Requisitos obligatorios del job diario, separados por comas (`piscina,padel`). |
| `RELEVANCE_THRESHOLD` | `0.6` | Umbral mínimo de relevancia para aceptar un anuncio. |
| `RESULTS_DIR` | `data/results` | Directorio de resultados JSON (vacío = solo memoria). |

## API

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/v1/searches` | Búsqueda síncrona. `201` + anuncios filtrados. `502` scraper, `503` clasificador, `422` validación. |
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

Sin llamadas reales a Apify ni DeepSeek: los puertos se sustituyen por fakes
(`app/tests/conftest.py`). Cubre servicio de búsqueda, job diario, normalización del
scraper, parseo del clasificador, endpoints y logging.

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
