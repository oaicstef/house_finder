# Plan: real-estate-search-agent

## Objetivo

Construir el backend de un agente de búsqueda inmobiliaria personalizada que scrapea portales (Idealista, Fotocasa) vía Apify, clasifica y puntúa los anuncios con un LLM (DeepSeek sobre el SDK de OpenAI) y devuelve solo las viviendas relevantes para los criterios del usuario. El sistema expone una API REST (búsqueda síncrona, consulta de resultados y disparo manual del job) y ejecuta además una búsqueda automática diaria a las 07:00 con idempotencia por fecha y criterio. Todo el código sigue Clean Architecture y SOLID, con logging estructurado enriquecido y manejo de excepciones tipado.

## Alcance

### Dentro del alcance
- Capa de dominio pura: entidades, excepciones de negocio y *ports* (interfaces) sin dependencias de frameworks.
- Implementaciones de infraestructura: scraper Apify (Idealista + Fotocasa), clasificador DeepSeek, repositorio en memoria.
- Orquestación de aplicación: `SearchService` (búsqueda síncrona) y `ScheduledSearchService` (job diario idempotente).
- API REST versionada `/api/v1` con endpoints delgados, middleware de contexto y handlers globales de error.
- Logging estructurado con `structlog` (JSON en prod, consola en dev) y `request_id` propagado por contexto.
- Configuración por entorno con `BaseSettings`, validada al arranque.
- Tests unitarios de servicios y de API con ports mockeados (sin llamadas reales a Apify/DeepSeek).
- `requirements.txt`, `requirements-dev.txt`, `.env.example`, `README.md`.

### Fuera del alcance (out of scope)
- Persistencia real (PostgreSQL/Redis): solo se implementa `InMemoryListingRepository`; la interfaz permite añadir otras sin tocar servicios.
- Autenticación/autorización, multi-tenant, cuotas y rate limiting.
- Frontend o UI; notificaciones por email/Telegram/WhatsApp.
- Normalización geográfica avanzada (geocoding), mapas o cálculo de distancias.
- Deduplicación entre portales (mismo piso publicado en Idealista y Fotocasa) y detección de anuncios fraudulentos.
- Reintentos con backoff y circuit breaker (se documentan como riesgo/mitigación futura).
- Migraciones de base de datos y despliegue en contenedores.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Lenguaje/runtime | Python 3.11+ | Requisito del stack; `asyncio` maduro y tipado moderno (`X \| None`, `Protocol`). |
| Framework HTTP | FastAPI | Inyección de dependencias nativa (`Depends`), validación Pydantic v2, documentación OpenAPI automática y handlers globales de excepción. |
| Validación/DTO | Pydantic v2 en la capa `api` (`schemas.py`) + `dataclasses` en dominio | Mantiene el dominio libre de frameworks (regla de dependencia) y da validación declarativa en el borde. |
| Configuración | `pydantic-settings` (`BaseSettings`) + `python-dotenv` | Una sola fuente de verdad tipada, carga de `.env` y validación al arranque con error de dominio en lugar de `KeyError`. |
| Scraping | `apify-client` ejecutado con `asyncio.to_thread` (proveedor por portal, ver [`specs/no-apify-scraping`](../no-apify-scraping/plan.md)) | Apify abstrae proxies/anti-bot; el cliente es síncrono, se aísla en un hilo para no bloquear el event loop. Desde `specs/no-apify-scraping` el proveedor se elige por portal (`SCRAPER_PROVIDERS`): Fotocasa usa HTTP propio y Apify queda como opcional/respaldo. |
| Clasificación | SDK `openai` apuntando a `https://api.deepseek.com`, `model="deepseek-flash"` | API compatible con OpenAI; permite cambiar de proveedor sin reescribir el port. Salida forzada a JSON para parseo determinista. |
| Scheduler | `APScheduler` `AsyncIOScheduler` + `CronTrigger(hour=7, minute=0)` | Coexiste con el event loop de FastAPI, se arranca/para en el `lifespan` y se desactiva con `ENABLE_SCHEDULER=false`. |
| Logging | `structlog` (`JSONRenderer` en prod, `ConsoleRenderer` en dev) + `contextvars` | Logs correlacionables por `request_id`/`search_id` sin pasar el logger por todas las firmas; JSON apto para agregadores. |
| Arquitectura | Clean Architecture (domain → application → infrastructure/api) + SOLID | Dominio aislado y testeable; los ports permiten OCP (nuevo portal = nueva implementación) y DIP (los servicios dependen de abstracciones). |
| Errores | Jerarquía `DomainException` con `code` + `http_status` | Traducción determinista a HTTP, `code` legible por máquina para el frontend y mensajes sin filtrar stack traces. |
| Estado | Repositorio en memoria con `asyncio.Lock` | Suficiente para esta iteración; el port permanece estable al añadir persistencia real. |
| Tests | `pytest` + `pytest-asyncio` + `httpx`/`TestClient`, dobles de test escritos a mano | Sin APIs reales, ejecución rápida y determinista; los fakes verifican el contrato de los ports. |

## Contratos de interfaces (ports)

`app/domain/ports.py` — `typing.Protocol` (tipado estructural, sin herencia obligatoria):

```python
from datetime import date
from typing import Protocol, Sequence
from uuid import UUID

from app.domain.models import (
    Listing, ListingAssessment, SearchCriteria, SearchRun, SearchResult,
)

class ScraperPort(Protocol):
    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        """Devuelve anuncios normalizados. Lanza ScraperException si un portal falla."""
        ...

class ClassifierPort(Protocol):
    async def assess_listings(
        self,
        criteria: SearchCriteria,
        listings: Sequence[Listing],
    ) -> list[ListingAssessment]:
        """Puntúa cada anuncio contra los criterios. Lanza ClassifierException."""
        ...

class ListingRepository(Protocol):
    async def save(self, result: SearchResult) -> None:
        """Persiste una ejecución de búsqueda completa (idempotente por search_id)."""
        ...

    async def get_by_id(self, search_id: UUID) -> SearchResult | None:
        """Devuelve None si no existe (el servicio traduce a SearchNotFoundError)."""
        ...

    async def find_by_criteria_and_day(
        self,
        criteria_fingerprint: str,
        run_date: date,
    ) -> SearchResult | None:
        """Soporte de idempotencia del job diario: None si no hay ejecución ese día."""
        ...
```

Contratos derivados usados por la capa API:

```python
class SearchServiceProtocol(Protocol):
    async def run_search(self, criteria: SearchCriteria, *, trigger: str = "api") -> SearchResult: ...
    async def get_results(self, search_id: UUID) -> SearchResult: ...

class ScheduledSearchServiceProtocol(Protocol):
    async def run_daily_job(self, *, raise_on_error: bool = True) -> SearchResult | None: ...
    async def scheduler_entrypoint(self) -> None: ...
```

## Modelo de datos

### Entidades de dominio (`app/domain/models.py`, stdlib `dataclasses`)

```python
class Portal(str, Enum):
    IDEALISTA = "idealista"
    FOTOCASA = "fotocasa"

@dataclass(frozen=True, slots=True)
class Listing:
    portal: Portal
    url: str                 # clave natural / deduplicación
    title: str
    price_eur: float
    size_m2: float | None
    rooms: int | None
    bathrooms: int | None
    location: str
    description: str | None
    raw: Mapping[str, Any]  # payload original del portal (no se serializa al cliente)

    @property
    def price_per_m2(self) -> float | None: ...

@dataclass(frozen=True, slots=True)
class SearchCriteria:
    location: str
    portals: tuple[Portal, ...] = (Portal.IDEALISTA, Portal.FOTOCASA)
    min_price_eur: float | None = None
    max_price_eur: float | None = None
    min_size_m2: float | None = None
    min_rooms: int | None = None
    max_results: int = 40
    preferences: str | None = None   # texto libre para el clasificador LLM

    def fingerprint(self) -> str: ...  # sha256 estable de los campos ordenados

@dataclass(frozen=True, slots=True)
class ListingAssessment:
    listing_url: str
    relevance: float        # 0.0 - 1.0
    matches: bool
    reason: str

@dataclass(frozen=True, slots=True)
class ScoredListing:
    listing: Listing
    relevance: float
    reason: str

@dataclass(frozen=True, slots=True)
class SearchResult:
    search_id: UUID
    criteria: SearchCriteria
    criteria_fingerprint: str
    run_date: date          # fecha local de ejecución (idempotencia)
    created_at: datetime    # UTC, tz-aware
    trigger: str            # "api" | "scheduler" | "manual-job"
    listings: tuple[ScoredListing, ...]
    scraped_count: int
    discarded_count: int
```

### Esquemas Pydantic v2 (`app/api/schemas.py`)

Petición:

```python
class SearchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    location: str = Field(min_length=2, max_length=120)
    portals: list[PortalLiteral] = ["idealista", "fotocasa"]
    min_price_eur: float | None = Field(default=None, ge=0)
    max_price_eur: float | None = Field(default=None, ge=0)
    min_size_m2: float | None = Field(default=None, gt=0)
    min_rooms: int | None = Field(default=None, ge=0, le=20)
    max_results: int = Field(default=40, ge=1, le=100)
    preferences: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")   # regla de negocio: rango coherente
    def _check_price_range(self) -> Self: ...

    def to_domain(self) -> SearchCriteria: ...
```

Respuesta:

```python
class ListingResponse(BaseModel):
    portal: str
    url: str
    title: str
    price_eur: float
    price_per_m2: float | None
    size_m2: float | None
    rooms: int | None
    bathrooms: int | None
    location: str
    relevance: float
    reason: str

class SearchResponse(BaseModel):
    search_id: UUID
    criteria_fingerprint: str
    run_date: date
    created_at: datetime
    trigger: str
    status: Literal["completed"]
    scraped_count: int
    matched_count: int
    discarded_count: int
    listings: list[ListingResponse]

class ScheduledRunResponse(BaseModel):
    executed: bool
    reason: str | None      # "already_executed_today" | "scheduler_disabled"
    result: SearchResponse | None

class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None

class ErrorResponse(BaseModel):
    error: ErrorDetail
```

## Endpoints de la API

Prefijo `/api/v1`. Todos los endpoints son delgados: solo validan, delegan en el servicio inyectado y mapean dominio → esquema.

| Método | Ruta | Request | Response | Errores |
|---|---|---|---|---|
| `POST` | `/api/v1/searches` | `SearchCreateRequest` | `201` + `SearchResponse` | `422` validación Pydantic (`VALIDATION_ERROR`), `502` `SCRAPER_FAILURE`, `503` `CLASSIFIER_FAILURE`, `500` `INTERNAL_ERROR` |
| `GET` | `/api/v1/searches/{search_id}/results` | path `UUID` | `200` + `SearchResponse` | `404` `SEARCH_NOT_FOUND`, `422` UUID inválido (`VALIDATION_ERROR`), `500` |
| `POST` | `/api/v1/scheduled-searches/run` | — | `200` + `ScheduledRunResponse` (`executed=false` si ya se ejecutó hoy) | `502`, `503`, `500` |
| `GET` | `/health` | — | `200` `{"status": "ok", "scheduler_enabled": bool}` | — |

Códigos de error: `422` validación de entrada, `404` recurso inexistente, `502` fallo del proveedor de scraping, `503` fallo del clasificador LLM, `500` error inesperado (mensaje genérico, sin stack trace).

## Flujo del job programado

1. `lifespan` de FastAPI construye el contenedor y, si `ENABLE_SCHEDULER=true`, arranca `AsyncIOScheduler`.
2. Se registra `scheduler_entrypoint` con `CronTrigger(hour=7, minute=0, timezone=<settings.timezone>)` e `id="daily-real-estate-search"`, `replace_existing=True`, `coalesce=True`, `max_instances=1`.
3. En cada disparo: `scheduler_entrypoint` envuelve **todo** en `try/except Exception` (una excepción no capturada en una tarea programada se pierde) y loguea `logger.exception(...)` con `error_code`; nunca propaga.
4. `run_daily_job` construye los criterios por defecto desde settings (`DAILY_SEARCH_LOCATION`, etc.) y calcula `criteria_fingerprint`.
5. **Idempotencia**: `repository.find_by_criteria_and_day(fingerprint, today)`. Si existe, se emite `scheduled_search_skipped_duplicate` a nivel `WARNING` con el `search_id` previo y el job termina sin error (`None`).
6. Si no existe, delega en `SearchService.run_search(criteria, trigger="scheduler")`.
7. `SearchService`: valida criterios → `bind_contextvars(search_id=...)` → `scraper.fetch_listings` → `classifier.assess_listings` → filtra `matches` y ordena por relevancia desc → persiste en el repositorio → log `search_completed` con `duration_ms`.
8. Errores de dominio durante el disparo: se capturan en el entrypoint, se loguean como `WARNING` con `error_code` y no relanzan. En la invocación manual (`raise_on_error=True`) se propagan para que la API devuelva `502`/`503`.
9. `shutdown` del `lifespan`: `scheduler.shutdown(wait=False)` si está activo.

## Estrategia de logging

`structlog` configurado en `app/infrastructure/logging_config.py`:

- Procesadores: `merge_contextvars` → `add_log_level` → `TimeStamper(fmt="iso", utc=True)` → `ensure_required_fields` → `StackInfoRenderer` → `format_exc_info` → `JSONRenderer` (prod) / `ConsoleRenderer` (dev).
- `ENVIRONMENT=production` ⇒ JSON; cualquier otro valor ⇒ consola legible.
- Middleware HTTP: genera `request_id` (UUID4, respeta `X-Request-ID` entrante), lo vincula con `structlog.contextvars.bind_contextvars(request_id=...)`, añade la cabecera `X-Request-ID`, mide `duration_ms` y limpia el contexto al finalizar (`clear_contextvars`).

Campos obligatorios en cada log (garantizados por el procesador `ensure_required_fields`, `null` si no aplican): `event`, `request_id`, `search_id`, `listing_url`, `duration_ms`, `error_code`.

| Evento | Nivel | Campos específicos |
|---|---|---|
| `app_startup` / `app_shutdown` | `INFO` | `environment`, `scheduler_enabled` |
| `http_request_completed` | `INFO` | `method`, `path`, `status_code`, `duration_ms` |
| `search_started` | `INFO` | `search_id`, `trigger`, `location`, `portals`, `max_results` |
| `listings_scraped` | `INFO` | `search_id`, `scraped_count`, `portals`, `duration_ms` |
| `listings_classified` | `INFO` | `search_id`, `scraped_count`, `matched_count`, `duration_ms` |
| `search_completed` | `INFO` | `search_id`, `matched_count`, `discarded_count`, `duration_ms` |
| `search_scraper_failed` | `WARNING` | `error_code=SCRAPER_FAILURE`, `portal` |
| `search_classifier_failed` | `WARNING` | `error_code=CLASSIFIER_FAILURE` |
| `scheduled_search_skipped_duplicate` | `WARNING` | `search_id`, `criteria_fingerprint`, `run_date` |
| `scheduled_search_failed` | `ERROR` (`logger.exception`) | `error_code` |
| `scheduler_started` / `scheduler_stopped` | `INFO` | `job_id`, `cron` |

Reglas: `WARNING` para errores de dominio esperados; `ERROR` con `logger.exception()` solo para lo inesperado; prohibido `except:` desnudo; nunca loguear tokens (`APIFY_TOKEN`, `DEEPSEEK_API_KEY`) ni payloads completos con datos personales.

## Jerarquía de excepciones

`app/domain/exceptions.py`:

| Clase | `code` | `http_status` | Cuándo |
|---|---|---|---|
| `DomainException(Exception)` | `DOMAIN_ERROR` | `400` | Base: `message`, `code`, `http_status`, `details` |
| `ValidationError(DomainException)` | `VALIDATION_ERROR` | `422` | Criterios incoherentes (p. ej. `min_price > max_price`) |
| `SearchNotFoundError(DomainException)` | `SEARCH_NOT_FOUND` | `404` | `search_id` inexistente |
| `ScraperException(DomainException)` | `SCRAPER_FAILURE` | `502` | Fallo/timeout del proveedor de scraping |
| `ClassifierException(DomainException)` | `CLASSIFIER_FAILURE` | `503` | Fallo del LLM o JSON inválido |
| `ConfigurationException(DomainException)` | `CONFIGURATION_ERROR` | `500` | Faltan claves obligatorias al arrancar |
| `RepositoryException(DomainException)` | `REPOSITORY_FAILURE` | `500` | Fallo del repositorio |

Handlers globales en `app/api/exception_handlers.py`: `DomainException` ⇒ su `http_status` + `ErrorResponse`; `RequestValidationError` ⇒ `422 VALIDATION_ERROR` con detalle de campos; `Exception` ⇒ `500 INTERNAL_ERROR` con mensaje genérico y `logger.exception` (el stack trace nunca llega al cliente).

## Plan de tests

Con mocks (unitarios, sin red):
- `SearchService.run_search`: filtrado por `matches`, orden por relevancia, `discarded_count`, persistencia en repositorio, `trigger` propagado, duración logueada.
- `SearchService.run_search`: `ScraperException`/`ClassifierException` se propagan sin persistir.
- `SearchService.get_results`: devuelve el resultado existente y lanza `SearchNotFoundError` si no existe.
- `SearchService`: criterios inválidos (`min_price_eur > max_price_eur`) ⇒ `ValidationError` sin invocar al scraper.
- `ScheduledSearchService.run_daily_job`: primera ejecución crea resultado; segunda el mismo día devuelve `None`, no llama al scraper y emite `WARNING` `scheduled_search_skipped_duplicate`.
- `ScheduledSearchService.scheduler_entrypoint`: cualquier excepción se captura y no se propaga (`logger.exception` invocado).
- `ApifyScraper`: normalización de items crudos a `Listing` y `ScraperException` ante error del cliente (cliente inyectado/monkeypatched).
- `DeepSeekClassifier`: parseo de JSON válido, tolerancia a JSON envuelto en ```json y `ClassifierException` ante respuesta inválida.
- `ensure_required_fields`: los seis campos obligatorios están presentes en cualquier log.

Con integración (TestClient, sin red, ports fake inyectados vía `dependency_overrides`):
- `POST /api/v1/searches` ⇒ `201` y cuerpo con `search_id`/`listings`.
- `GET /api/v1/searches/{id}/results` ⇒ `200` para un id conocido y `404` `SEARCH_NOT_FOUND` para uno desconocido.
- `POST /api/v1/searches` con body inválido ⇒ `422` `VALIDATION_ERROR`.
- `POST /api/v1/scheduled-searches/run` ⇒ `executed=true` la primera vez y `executed=false` la segunda.
- `GET /health` ⇒ `200`.
- Cabecera `X-Request-ID` presente en la respuesta.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Actores de Apify renombrados/deprecados o límite de cuota agotado | Búsquedas vacías o `502` | IDs de actor y `APIFY_MAX_ITEMS` configurables por entorno; `ScraperException` tipada; tests con cliente fake (no dependen de red). |
| Respuesta del LLM no es JSON válido o alucina URLs | Clasificación incorrecta / `503` | `response_format={"type": "json_object"}` + prompt estricto, validación de `listing_url` contra el set scrapeado, descarte de entradas desconocidas, `ClassifierException` ante fallo total. |
| Coste/latencia: N anuncios por búsqueda síncrona | Timeout del cliente HTTP | `max_results` acotado (1-100), una sola llamada al LLM por búsqueda (batch), `timeout` configurable en ambos clientes. |
| Job diario duplicado (reinicio del proceso, doble réplica) | Resultados y coste duplicados | Idempotencia `(criteria_fingerprint, run_date)`, `coalesce=True`, `max_instances=1`; el flag `ENABLE_SCHEDULER` permite un único nodo con scheduler. |
| Repositorio en memoria se pierde al reiniciar | `GET /results` devuelve `404` tras un reinicio | Port `ListingRepository` estable: sustituir por Postgres/Redis sin tocar servicios; documentado como limitación conocida. |
| Fuga de secretos en logs | Riesgo de seguridad | Nunca se loguean tokens; el payload `raw` no se serializa en respuestas ni en logs. |

## Orden de implementación

1. [x] **Scaffolding y dependencias** — `requirements.txt`, `requirements-dev.txt`, `.env.example`, `.gitignore`, `app/__init__.py` y `__init__.py` de cada paquete — *done*: `pip install -r requirements.txt` resuelve y `import app` funciona.
2. [x] **Dominio: excepciones y modelos** — `app/domain/exceptions.py`, `app/domain/models.py` — *done*: `fingerprint()` es estable entre instancias equivalentes; jerarquía con `code`/`http_status` importable sin dependencias externas.
3. [x] **Dominio: ports** — `app/domain/ports.py` — *done*: `ScraperPort`, `ClassifierPort`, `ListingRepository` y protocolos de servicio definidos; los fakes de test los satisfacen.
4. [x] **Configuración** — `app/config.py` — *done*: `get_settings()` lanza `ConfigurationException` (no `KeyError`/`ValidationError`) si faltan `APIFY_TOKEN`/`DEEPSEEK_API_KEY`.
5. [x] **Logging estructurado** — `app/infrastructure/logging_config.py` — *done*: `configure_logging()` renderiza JSON en prod/consola en dev y todos los eventos incluyen los 6 campos obligatorios.
6. [x] **Repositorio en memoria** — `app/infrastructure/repositories/memory_repository.py` — *done*: `save`/`get_by_id`/`find_by_criteria_and_day` con `asyncio.Lock` y tests implícitos vía servicios.
7. [x] **Scraper Apify** — `app/infrastructure/scrapers/apify_scraper.py` — *done*: normaliza items a `Listing`, filtra por criterios duros baratos y lanza `ScraperException` ante fallo.
8. [x] **Clasificador DeepSeek** — `app/infrastructure/classifiers/deepseek_classifier.py` — *done*: JSON parseado y validado; `ClassifierException` ante error o payload inválido.
9. [x] **Servicios de aplicación** — `app/application/services/search_service.py`, `app/application/services/scheduled_search.py` — *done*: orquestan ports, aplican filtrado/orden/idempotencia y loguean métricas.
10. [x] **Capa API** — `app/api/schemas.py`, `app/api/dependencies.py`, `app/api/middleware.py`, `app/api/exception_handlers.py`, `app/api/routes.py` — *done*: endpoints delgados con `Depends`, handlers globales y middleware `request_id`.
11. [x] **Composición y arranque** — `app/main.py` — *done*: `create_app()`, `lifespan` con scheduler condicional, `uvicorn app.main:app --reload` levanta.
12. [x] **Tests** — `tests/conftest.py`, `tests/test_search_service.py`, `tests/test_scheduled_search.py`, `tests/test_apify_scraper.py`, `tests/test_deepseek_classifier.py`, `tests/test_api.py`, `tests/test_logging_config.py` — *done*: `pytest` en verde sin acceso a red.
13. [x] **Documentación** — `README.md` — *done*: instalación, variables, endpoints, ejecución y tests documentados.

## Criterios de aceptación

- [x] `specs/real-estate-search-agent/plan.md` completo antes de cualquier archivo de código de la feature.
- [x] `requirements.txt`, `.env.example` y `README.md` presentes y coherentes con el código.
- [x] Tests unitarios de `SearchService` con ports mockeados; ninguna llamada real a Apify ni DeepSeek.
- [x] Comentarios en español; nombres de clases, funciones y variables en inglés.
- [x] `uvicorn app.main:app --reload` arranca con `.env` válido (o falla con `ConfigurationException` claro si faltan claves).
- [x] Logging JSON en producción y consola en desarrollo; todo evento incluye `event`, `request_id`, `search_id`, `listing_url`, `duration_ms`, `error_code`.
- [x] `POST /api/v1/searches` devuelve `201` con listings filtrados y `502`/`503` ante fallos de scraper/clasificador.
- [x] `GET /api/v1/searches/{search_id}/results` devuelve `200` o `404` `SEARCH_NOT_FOUND`.
- [x] `POST /api/v1/scheduled-searches/run` es idempotente en el mismo día (`executed=false` + `WARNING`).
- [x] `ENABLE_SCHEDULER=false` desactiva el cron; con `true` el job se registra a las 07:00.
- [x] `pytest` en verde.

## Checklist de progreso

- [x] 1. Scaffolding y dependencias (`requirements*.txt`, `.env.example`, `.gitignore`, `__init__.py`)
- [x] 2. Dominio: excepciones y modelos (`app/domain/exceptions.py`, `app/domain/models.py`)
- [x] 3. Dominio: ports (`app/domain/ports.py`)
- [x] 4. Configuración (`app/config.py`)
- [x] 5. Logging estructurado (`app/infrastructure/logging_config.py`)
- [x] 6. Repositorio en memoria (`app/infrastructure/repositories/memory_repository.py`)
- [x] 7. Scraper Apify (`app/infrastructure/scrapers/apify_scraper.py`)
- [x] 8. Clasificador DeepSeek (`app/infrastructure/classifiers/deepseek_classifier.py`)
- [x] 9. Servicios de aplicación (`app/application/services/*.py`)
- [x] 10. Capa API (`app/api/*.py`)
- [x] 11. Composición y arranque (`app/main.py`)
- [x] 12. Tests (`tests/*.py`)
- [x] 13. Documentación (`README.md`)

## Revisión 2026-11-04

Cambios surgidos durante la implementación (el resto del plan permanece intacto):

1. **`SEARCH_OPERATION` (`rent` | `sale`) en `Settings`** — el actor de Apify necesita saber
   si la búsqueda es de alquiler o de compraventa. Se añade la variable en lugar de
   fijarla en el constructor del scraper (`apify_scraper.operation`).
2. **Parseo numérico robusto en el scraper** — el primer intento eliminaba todo lo que no
   fuera dígito/separador, de modo que `"80 m2"` se convertía en `802.0`. Ahora se extrae
   solo el primer token numérico (`_NUMBER_TOKEN`) y los separadores de miles españoles
   (`"1.250 €"` -> `1250.0`) se interpretan correctamente. Detectado por
   `test_normalizes_raw_item_to_domain_listing`.
3. **`Listing.price_per_m2` con redondeo comercial** — se usa `Decimal` +
   `ROUND_HALF_UP` en vez de `round()` (bancario), porque es un valor mostrado al usuario:
   `1250/80` -> `15.63`, no `15.62`.
4. **`status.HTTP_422_UNPROCESSABLE_ENTITY` deprecado** en Starlette reciente; el mapa de
   respuestas de OpenAPI usa la clave `422` literal para evitar el warning.
5. **`asyncio_default_fixture_loop_scope = function`** en `pytest.ini` para eliminar el
   `PytestDeprecationWarning` de `pytest-asyncio`.

Evidencia de validación: `pytest` -> 63 passed; arranque con `uvicorn` verificado
(`/health`, `/docs`, `/openapi.json`, `422` y `404` con cuerpo `{"error": {...}}`);
scheduler registrado como `cron[hour='7', minute='0']` con zona `Europe/Madrid`;
`ConfigurationException` verificada al arrancar sin credenciales en `development`.

## Revisión 2026-11-05

Cambios derivados de validar el adaptador contra el **SDK real** (`apify-client` 3.4.0) y de
la pregunta sobre el coste de Apify:

1. **`apify-client` fijado a `>=3.0.0,<4.0.0`.** El rango anterior (`>=1.7.0`) permitía v1/v2,
   cuya firma de `call()` es incompatible con la que usa el adaptador.
2. **Corregido `ActorClient.call()`:** el parámetro `timeout_secs` **no existe** en v3 (se
   llamaba `timeout_secs`, ahora `run_timeout: timedelta`). La llamada anterior habría
   fallado con `TypeError`.
3. **Corregido el acceso al dataset:** en v3 `call()` devuelve un modelo `Run`, **no un dict**,
   por lo que `run.get("defaultDatasetId")` lanzaba `AttributeError`. Ahora se leen los
   helpers `_dataset_id()` / `_run_status()`, que soportan el modelo v3
   (`run.default_dataset_id`) y, por compatibilidad, diccionarios con `defaultDatasetId`.
4. **Estados de run fallidos** (`FAILED`, `ABORTED`, `TIMED-OUT`) se traducen a
   `ScraperException` con el `status` en `details`, en lugar de intentar leer un dataset vacío.
5. **Nuevo `APIFY_MAX_CHARGE_USD`** (`Settings.apify_max_charge_usd` -> `max_total_charge_usd`
   como `Decimal`): tope duro de gasto por ejecución, alineado con el modelo de precios de
   Apify descrito en la guía.
6. **Los fakes de test imitan ahora la forma real de v3** (objeto `Run` con
   `default_dataset_id`), que es lo que habría detectado los puntos 2 y 3; se conserva un test
   para la variante dict legacy.
7. **Valor vacío en `.env` -> `None`** (`_blank_to_none`): `DAILY_MAX_PRICE_EUR=` ya no rompe
   la carga de `Settings`.

Evidencia: `pytest` -> 69 passed; verificación adicional ejecutando el scraper contra un
`Run` real de `apify_client._models` (dataset leído correctamente, `run_timeout` de 300 s,
`max_total_charge_usd` como `Decimal`, sin `timeout_secs`).

## Revisión 2026-11-05 (b) — validación contra Apify y DeepSeek reales

Primera ejecución contra las APIs reales (cuenta del usuario). Hallazgos y cambios:

1. **`_build_run_input` no coincidía con ningún actor real.** Faltaban `propertyType` y
   `country` (obligatorios en `igolaizola/idealista-scraper`) y se enviaba `minRooms`, que
   ese actor no admite: el input se rechazaba. Ahora el input se construye por portal con
   `_PortalProfile`, y `min_rooms` se aplica en `_matches_hard_criteria`.
2. **Tipos por actor.** Idealista exige `minPrice`/`maxPrice`/`minSize` como **string**;
   Fotocasa los espera **enteros** y usa `buy`/`home` en lugar de `sale`/`homes`. Un input
   único no podía servir a ambos; de ahí los perfiles.
3. **`property_type`/`country` como settings** se eliminaron: quedaron redundantes al
   resolverse por perfil. No se añade configuración muerta.
4. **Portales sin actor se omiten** (`scraper_portal_skipped`, WARNING) en vez de lanzar.
   Antes, tener `APIFY_FOTOCASA_ACTOR` sin configurar tumbaba la búsqueda completa. Si
   ninguno está configurado, se mantiene `ScraperException`.
5. **El LLM ya no decide la aceptación.** Con `matches` en manos del modelo, cada
   preferencia se trataba como excluyente y se rechazaban chalets con piscina por «no constar
   pista de pádel» (0 de 12 aceptados con puntuaciones 0.22-0.68). Ahora el modelo **solo
   puntúa** y el sistema compara con `RELEVANCE_THRESHOLD`: determinista y ajustable por
   entorno. El prompt incorpora una escala explícita y aclara que las preferencias son
   deseables, no excluyentes.
6. **`Listing.amenities`**: el actor expone `features.hasSwimmingPool/hasGarden/hasTerrace/
   hasAirConditioning/hasLift/hasBoxRoom`. Se normalizan a `swimming_pool`, `garden`, etc.,
   se envían al LLM como datos verificados y se devuelven en la API (`ListingResponse`).
   Es lo que permite responder con rigor a «con piscina».
7. **`title` anidado**: el actor no tiene `title` plano (está en `suggestedTexts.title`) y
   la ubicación vive en `address`. Ambos se extraen; antes el título salía como
   «Vivienda sin título».

Evidencia: `pytest` -> 78 passed. Ejecución real de la búsqueda del usuario
(chalet, ≤900.000 €, 4+ hab., piscina y pádel, Pozuelo de Alarcón):
`scraped=12, matched=4`, primer resultado chalet adosado de 875.000 €, 210 m², 4 hab., con
`swimming_pool` y `relevance=0.78`. La misma consulta sobre «Madrid Capital» devuelve
`matched=0` de forma legítima: en el centro no hay chalets con piscina.

## Revisión 2026-11-05 (c) — persistencia de resultados

Los resultados solo vivían en memoria, así que `GET /searches/{id}/results` devolvía `404`
tras reiniciar (comportamiento reprodu­cido: guardar en un proceso y leer en otro daba
`None`). Para un agente que busca a diario eso hacía inservible el histórico.

1. **Nuevo `JsonFileListingRepository`** (`app/infrastructure/repositories/`): un fichero
   `<RESULTS_DIR>/<search_id>.json` por búsqueda, con **los enlaces** (`url`), puntuación,
   motivo y amenities de cada anuncio.
2. **Índice reconstruido al arrancar**: `get_by_id` y `find_by_criteria_and_day` siguen
   funcionando tras un reinicio, por lo que **la idempotencia del job diario se conserva**.
3. **Escritura atómica** (temporal + `os.replace`): nunca queda un JSON a medias. Un
   fichero corrupto se ignora con `WARNING` sin impedir el arranque.
4. **`Listing.raw` no se persiste**: es voluminoso y puede contener datos de contacto.
5. **`RESULTS_DIR`** (`Settings.results_dir`, por defecto `data/results`; vacío = solo
   memoria). `build_container` elige el adaptador; ni servicios ni endpoints cambian (OCP).
6. `data/` añadido a `.gitignore`.

Evidencia: `pytest` -> 88 passed (10 nuevos del repositorio). Verificación real: búsqueda
`201` que escribe `data/results/0c9ba77a-….json` con los dos enlaces de Idealista; una
**segunda instancia** de la app (reinicio simulado) responde `GET /results` con `200` y los
mismos enlaces, y `find_by_criteria_and_day` devuelve el resultado previo.

## Revisión 2026-11-05 (d) — requisitos obligatorios (`must_have`)

El usuario reportó que, pidiendo «Chalet, padel, piscina», el resultado no tenía pista de
pádel. Diagnóstico: (a) todo era "preferencia" blanda, así que el umbral aceptaba anuncios
sin pádel; y (b) un fallo de recorte hacía que apenas se exploraran candidatos.

1. **Causa raíz del recorte**: `fetch_listings` terminaba con
   `listings[: criteria.max_results]`, y además `maxItems` se calculaba como
   `min(max_results, apify_max_items)`. Con `max_results: 5` se pedían **5** anuncios al
   actor y se recortaban a **5** antes de filtrar (el actor devolvía 53 y 30 pasaban el
   filtro duro). Ahora el scraper pide `APIFY_MAX_ITEMS` y **no trunca**: `max_results`
   solo recorta la respuesta final en `SearchService`.
2. **Nuevo `must_have: list[str]`** en `SearchCreateRequest` -> `SearchCriteria.must_have`.
   Filtro duro (AND) aplicado antes del LLM: descarta el anuncio si no menciona el término
   en título, descripción o comodidades etiquetadas. Evita gastar tokens en lo excluido.
3. **`Listing.satisfies_all` / `normalize_text`** (dominio puro): comparación sin acentos
   ni mayúsculas, y `swimming_pool` cuenta como «piscina» vía `AMENITY_LABELS`. Así
   `"padel"` encuentra `"pista pádel"` y `"piscina"` encuentra el flag del portal.
4. **`must_have` entra en el fingerprint**: cambiar los requisitos invalida la idempotencia
   del día, como debe ser.
5. **`verified_requirements` en el prompt**: se indica al LLM qué requisitos ya se han
   comprobado, para que el `reason` no diga «falta pista de pádel» cuando sí la tiene.
6. **Persistencia**: `must_have` se serializa en el JSON del repositorio.

Evidencia: `pytest` -> 110 passed (22 nuevos). Petición real del usuario con
`must_have: ["padel", "piscina"]` sobre Madrid: `scraped=30, matched=2`, ambos chalets
adosados en Pau de Carabanchel (287 m² / 897.000 € y 225 m² / 898.000 €) con
`relevance=1.0` y motivo «tiene piscina y pádel verificados». Sin `must_have`, el mismo
filtro devolvía un chalet sin pádel con `relevance=0.85`.

## Revisión 2026-11-05 (e) — requisitos obligatorios en el job diario (`DAILY_MUST_HAVE`)

`must_have` solo existía en `POST /api/v1/searches`; la búsqueda diaria programada no
tenía forma de exigirlo, así que el único camino era escribir «(Obligatorio)» dentro de
`DAILY_PREFERENCES`, donde el LLM lo trataba como una preferencia blanda. Es la misma
causa raíz que la revisión (d), pero en la configuración del cron.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Transporte del requisito | `DAILY_MUST_HAVE`, lista separada por comas | Coherente con `DAILY_SEARCH_PORTALS`; no rompe `.env` existentes (opcional, vacío = sin filtro). |
| Dónde se parsea | `Settings.build_daily_criteria()` | El cron solo consume `SearchCriteria`; ni `ScheduledSearchService` ni los endpoints cambian (OCP). |
| Validación | Términos de 2-40 caracteres, sin duplicados (case-insensitive) | Mismo contrato que `SearchCreateRequest.must_have`; un valor inválido falla al arrancar con `ConfigurationException`. |
| Idempotencia | `must_have` ya forma parte de `SearchCriteria.fingerprint()` | Cambiar los requisitos diarios genera una huella nueva y permite una ejecución distinta el mismo día. |

### Alcance

- Dentro: `app/config.py`, `.env.example`, `README.md`, `docs/USAGE.md`, tests de config.
- Fuera: cambios en el esquema HTTP, en el scraper o en el prompt del clasificador (ya
  soportan `must_have` desde la revisión (d)); UI para editar criterios.

### Plan de tests

- `app/tests/test_config.py`: parseo con espacios/mayúsculas/duplicados, vacío -> `()`,
  término demasiado corto o largo -> `ConfigurationException`, y huella distinta al
  cambiar `DAILY_MUST_HAVE`.
- Sin red: se instancia `Settings(_env_file=None, environment="test", ...)`.

### Orden de implementación

1. [x] `app/config.py` — campo `daily_must_have`, parseo y paso a `SearchCriteria` — *done* cuando `build_daily_criteria()` devuelve la tupla limpia.
2. [x] `.env.example` + `README.md` + `docs/USAGE.md` — documentar la variable — *done* cuando aparece en la tabla y en la sección 10.
3. [x] `app/tests/test_config.py` — cobertura del parseo y del fingerprint — *done* con `pytest` en verde.

### Criterios de aceptación

- [x] `DAILY_MUST_HAVE=piscina,padel` hace que el cron descarte anuncios sin esos términos.
- [x] `DAILY_MUST_HAVE=` (vacío) mantiene el comportamiento anterior sin filtro duro.
- [x] Un requisito inválido impide arrancar con un mensaje claro, no con `KeyError`.
- [x] `pytest` en verde.

### Evidencia

`pytest` -> 117 passed (7 nuevos en `app/tests/test_config.py`). Verificación de
comportamiento con el job real y fakes: criterios con `DAILY_MUST_HAVE=piscina,padel`
sobre 2 anuncios (`con piscina y padel` / `solo piscina`) -> `scraped=2, matched=1`; la
segunda ejecución del mismo día devuelve `None` (idempotencia intacta). Términos vacíos
siguen produciendo `must_have=()` (sin filtro duro).

## Revisión 2026-11-05 (f) — activar Fotocasa: normalización de salida y defaults seguros

El portal de Fotocasa estaba desactivado «hasta validar su salida». Dos defectos lo
impedían y uno de ellos era silencioso:

1. **Precio anidado**: el actor no devuelve `price` en la raíz sino `transaction.price`,
   así que `_normalize` descartaba *todos* los items (precio `None`) y el portal habría
   costado dinero sin devolver un solo anuncio.
2. **Valor por defecto engañoso**: `apify_fotocasa_actor = "fotocasa-scraper"` no existe en
   Apify (`404`), así que **borrar** la variable del `.env` rompía la búsqueda entera
   (`502 SCRAPER_FAILURE`), mientras que dejarla vacía omitía el portal. Efecto
   contra-intuitivo: quitar una línea opcional tumbaba Idealista.

### Evidencia real (una ejecución, `maxTotalChargeUsd=0.05`, `maxItems=3`)

Item real de `igolaizola/fotocasa-scraper` (build `zQ6P2E7A5hwEubfcJ`, `SUCCEEDED`):

| Campo del actor | Valor observado | Campo del dominio |
|---|---|---|
| `url` | `https://www.fotocasa.es/es/comprar/vivienda/madrid-capital/ventas/191310510/d` | `url` (ya funcionaba) |
| `transaction.price` | `70000` (entero) | `price_eur` (**no se leía**) |
| `surface` | `59884` (m²) | `size_m2` |
| `rooms` / `baths` | `5` / `3` | `rooms` / `bathrooms` (**`baths` no estaba**) |
| `street` + `number` | `Calle Montejurra` + `33` | `title` (compuesto) |
| `location.level8Name` + `level7Name` + `level5Name` | `Ventas`, `Ciudad Lineal`, `Madrid` | `location` (**antes se serializaba el dict**) |
| `description` | texto libre | `description` |
| `features` | `[]` (lista vacía) | `amenities` = `()` |
| `features` (chalets) | `[{"id": "17"}, …]` (solo ids) | `amenities` desde el slug de la URL |
| `url` de un chalet | `…/pozuelo-de-alarcon/aire-acondicionado-calefaccion-parking-jardin-terraza-trastero-piscina/190094194/d` | `{air_conditioning, garden, terrace, storage_room, swimming_pool}` |

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Rutas anidadas | `_first_value` resuelve rutas con puntos (`transaction.price`) | Evita un normalizador paralelo por portal; el resto de alias sigue plano. |
| Título/ubicación sin campo propio | `_PortalProfile.title_builder` / `location_builder` opcionales | La variación de *salida* vive en el perfil del portal (OCP), igual que la de *entrada*. |
| Amenities de Fotocasa | No se inventan: `()` si el actor no las expone | `must_have` sigue funcionando porque compara contra `description`. |
| Amenities desde el slug | `_url_amenities` reutiliza `AMENITY_LABELS` sobre la URL | Verificado en vivo: `features` solo trae `{"id": "17"}` y el slug enumera `…-jardin-terraza-trastero-piscina`. Sin vocabulario nuevo: los mismos sinónimos del dominio sirven para `must_have`. |
| Defaults | `APIFY_IDEALISTA_ACTOR=igolaizola/idealista-scraper` (real) y `APIFY_FOTOCASA_ACTOR=` vacío | Un default debe ser un valor que funcione; «vacío = omitir» es seguro y el 404 nunca sorprende. |

### Alcance

- Dentro: `apify_scraper.py` (alias, builders, perfil), `config.py` (defaults),
  `.env.example`, `README.md`, `docs/USAGE.md`, tests.
- Fuera: `features`/amenities de Fotocasa más allá de lo que publique el actor, filtros
  específicos del portal (`locationType`, `publicationDate`, `sortBy`), dedupe entre
  portales.

### Plan de tests

- `app/tests/test_apify_scraper.py`: item real de Fotocasa saneado -> `price_eur`,
  `bathrooms`, `title` compuesto, `location` de texto, `amenities == ()`; item sin `url`
  se descarta; `transaction.price` ausente -> item descartado (no excepción).
- `app/tests/test_config.py`: default de Fotocasa vacío y de Idealista con formato
  `usuario/actor`; con el default vacío, una búsqueda solo-Fotocasa omite el portal.

### Orden de implementación

1. [x] `apify_scraper.py` — alias `transaction.price` + `baths`, rutas anidadas, builders por portal — *done* cuando un item real de Fotocasa se normaliza con precio, baños, título y ubicación.
2. [x] `config.py` — defaults `igolaizola/idealista-scraper` y `""` — *done* cuando borrar `APIFY_FOTOCASA_ACTOR` ya no produce 404.
3. [x] `.env.example`, `README.md`, `docs/USAGE.md` — cómo activar Fotocasa y su coste — *done* cuando la variable aparece con el valor recomendado.
4. [x] tests — *done* con `pytest` en verde.

### Criterios de aceptación

- [x] Un item real de Fotocasa produce un `Listing` con `url`, `price_eur`, `size_m2`, `bathrooms` y `location` legible.
- [x] `APIFY_FOTOCASA_ACTOR=` sigue omitiendo el portal con `WARNING` (sin gastar).
- [x] Sin la variable, el comportamiento es el mismo que vacía (nunca `404`).
- [x] `pytest` en verde.

### Evidencia

- `pytest` -> 123 passed (6 nuevos: 5 de Fotocasa en `test_apify_scraper.py`, 1 de defaults
  en `test_config.py`).
- **Datos reales ya descargados** (3 items, sin coste extra) a través del scraper real:
  `listings normalizados: 3 de 3` (antes 0/3 por el precio anidado), con
  `70000 € / 59884 m² / 5 hab / 3 baños` y título
  `Calle Montejurra 33, Ventas, Ciudad Lineal, Madrid`.
- **Ejecución real por el código de la app** (`ApifyListingScraper`, `maxItems=3`,
  `maxTotalChargeUsd=0.05`): 3 anuncios con enlace, precio y baños, p. ej.
  `598000 € / 61 m² / 2 hab / 1 baño — Cl. de Ayala, Goya, Barrio de Salamanca, Madrid`.
- **Chalets reales** (`Pozuelo de Alarcón`, `minSize=200`): el slug confirma el hallazgo
  `…-jardin-terraza-trastero-piscina/…`, base de `_url_amenities`.

## Revisión 2026-11-05 (g) — ubicación por portal y visibilidad de resultados parciales

Reporte del usuario: con `"location": "Madrid, Madrid"` la respuesta traía **solo**
anuncios de Fotocasa, ninguno de Idealista, y sin ningún aviso. Reproducido con los
resultados persistidos: `scraped=20, matched=5`, los 5 de Fotocasa; las búsquedas
anteriores con `"Madrid"` sí devolvían 30 candidatos de Idealista.

### Causa raíz (verificada ejecutando el actor)

El actor de Idealista **no resuelve** `"Madrid, Madrid"` ni `"madrid-capital"`:

```
ERROR Couldn't resolve locations error="location \"Madrid, Madrid\" not found"
```

Y aun así termina con `status: SUCCEEDED`, `exitCode: 0` y **0 items**. Para el scraper
fue un éxito vacío: no hay excepción, no hay `WARNING` y el cliente recibe `201` con
resultados parciales indistinguibles de una búsqueda completa. Fotocasa sí resuelve
`"Madrid, Madrid"` (usa un buscador de sugerencias), de ahí la asimetría.

| `location` enviada | Idealista | Fotocasa |
|---|---|---|
| `Madrid` | ✅ 2/2 items (`112998509`, 750 000 €, 325 m²) | ✅ |
| `Madrid, Madrid` | ❌ 0 items, `SUCCEEDED` | ✅ |
| `madrid-capital` | ❌ 0 items, `SUCCEEDED` | — |

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Formato de ubicación | `_PortalProfile.location_transform`: Idealista recibe el primer segmento (`"Madrid, Madrid"` → `"Madrid"`) | El formato `"Ciudad, Provincia"` es el que sugiere Fotocasa; el actor de Idealista solo entiende el nombre. La variación vive en el perfil del portal, como la de entrada/salida. |
| Portal sin resultados | `WARNING` `scraper_portal_returned_no_listings` con la ubicación resuelta | Convierte un fallo silencioso en un evento accionable; es el único punto que conoce los items **crudos**. |
| Visibilidad para el cliente | `SearchResult.portal_outcomes` (derivado, sin persistir) -> `portals` y `warnings` en `SearchResponse` | Se calcula de `criteria.portals` + `listings`, así que no cambia el contrato del puerto ni el JSON guardado (compatible hacia atrás). |
| ¿Lanzar error si un portal devuelve 0? | No | Una búsqueda legítimamente vacía (p. ej. `must_have: ["padel"]`) no es un fallo; se informa, no se rompe. |

### Alcance

- Dentro: `apify_scraper.py` (transformación de ubicación + warning), `models.py`
  (`PortalOutcome` + propiedad), `search_service.py` (warning por portal), `api/schemas.py`
  (`portals`, `warnings`), docs y tests.
- Fuera: reintentos automáticos con otra ubicación (coste duplicado), geocodificación
  propia, cambios en el contrato de `ScraperPort`.

### Plan de tests

- `test_apify_scraper.py`: Idealista recibe `"Madrid"` cuando se pide `"Madrid, Madrid"`;
  Fotocasa recibe la ubicación tal cual; `WARNING` cuando el actor devuelve 0 items;
  el `INFO` incluye la ubicación resuelta.
- `test_search_service.py`: `portal_outcomes` y `WARNING` cuando un portal no aporta.
- `test_api.py`: `portals`/`warnings` en la respuesta `201`.
- `test_json_file_repository.py`: un JSON antiguo sin esos campos sigue cargando.

### Orden de implementación

1. [x] `apify_scraper.py` — `location_transform` + logs — *done* cuando `"Madrid, Madrid"` llega como `"Madrid"` al actor de Idealista.
2. [x] `models.py` + `search_service.py` — `PortalOutcome`, advertencias — *done* cuando el servicio registra el portal vacío.
3. [x] `api/schemas.py` — `portals` + `warnings` — *done* cuando la respuesta los incluye.
4. [x] docs + tests — *done* con `pytest` en verde.

### Criterios de aceptación

- [x] Una búsqueda con `"Madrid, Madrid"` incluye anuncios de Idealista.
- [x] Si un portal devuelve 0, la respuesta lo delata (`portals`/`warnings` y `WARNING` en logs).
- [x] Un JSON de resultados antiguo sigue leyéndose.
- [x] `pytest` en verde.

### Evidencia

- `pytest` -> 130 passed (7 nuevos).
- **Ejecución real con el body del usuario** (`"Madrid, Madrid"`, 900 000 €, 200 m², 4 hab.,
  ambos portales, `maxItems=20`, `maxTotalChargeUsd=0.15`):

  ```
  listings_scraped portal=idealista location=Madrid        scraped_count=20
  listings_scraped portal=fotocasa  location='Madrid, Madrid' scraped_count=20
  TOTAL: 24 | idealista: 14 | fotocasa: 10
  ```

  Antes del cambio, Idealista devolvía 0 (actor `SUCCEEDED` con `location not found`).
- **Retrocompatible**: el JSON guardado de la búsqueda fallida (`ce23002a…`) carga sin
  cambios y ahora se autodiagnostica: `[('idealista', 0), ('fotocasa', 5)]` +
  `idealista: sin anuncios; revisa el actor del portal y la ubicación`.

## Revisión 2026-11-11 — Mostrar si el anuncio es obra nueva o segunda mano

### Objetivo

Exponer en los resultados si el portal identifica explícitamente el inmueble como obra
nueva o de segunda mano. El dato será opcional: si no se publica o no se puede interpretar
con seguridad, se devuelve `null` y no se muestra una etiqueta engañosa.

### Alcance

#### Dentro del alcance
- Añadir `listing_condition` al modelo de dominio y a `ListingResponse`.
- Normalizar indicadores explícitos conocidos en la respuesta cruda de los portales.
- Mostrar “Obra nueva” o “Segunda mano” en cada tarjeta cuando el dato esté disponible.
- Cubrir valores booleanos/textuales, ausencia y compatibilidad del contrato con tests.

#### Fuera del alcance (out of scope)
- Filtros de búsqueda por estado del inmueble.
- Inferir el estado por año de construcción, descripción genérica o IDs de catálogo no
  documentados.
- Cambios en persistencia de criterios, scheduler o proveedor de scraping.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Valores del dominio/API | `new`, `second_hand` o `null` | Contrato estable y explícito sin mezclar etiquetas de UI |
| Interpretación | Solo indicadores explícitos y reconocidos | Evita clasificar incorrectamente anuncios incompletos |
| Presentación | Etiqueta localizada en la tarjeta | El dato describe el anuncio, no es un filtro |

### Contratos de interfaces (ports)

No se modifican los ports. `Listing` añade `listing_condition: Literal["new", "second_hand"] | None`;
`ListingResponse` serializa el mismo campo.

### Modelo de datos

Campo aditivo `listing_condition` en la entidad de dominio y respuesta Pydantic. Se aceptan
solo dos valores; ausencia o señal desconocida se representa como `null`. Los resultados
guardados antiguos deben seguir deserializándose.

### Endpoints de la API

Sin rutas ni códigos de error nuevos. Las respuestas existentes de búsqueda incluyen
`listing_condition` por anuncio (`new`, `second_hand` o `null`).

### Flujo del job programado

Sin cambios: el scheduler consume el mismo `ScraperPort` y persiste anuncios con el campo
opcional; no se usa el dato para idempotencia ni filtrado.

### Estrategia de logging

Sin nuevos eventos: el estado se entrega como dato del anuncio, no como condición operativa.
No se registran descripciones ni datos personales.

### Jerarquía de excepciones

Sin cambios: valores no reconocidos se normalizan a `null`, no son errores de negocio.

### Plan de tests

- Tests unitarios de normalización para indicador verdadero/falso, alias textuales,
  valores desconocidos y ausencia del dato.
- Test de `ListingResponse.from_domain` para serializar el valor.
- Test de tarjeta/renderizado si hay infraestructura de tests web; en su ausencia, validar
  el módulo JS y conservar la etiqueta oculta cuando el valor es `null`.
- Ejecutar la suite de pytest y los checks disponibles del frontend.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Los portales usan campos diferentes o no publican el estado | El dato puede faltar | Campo opcional; agregar alias solo con evidencia |
| `false` significa “no es promoción de obra nueva” y no estado de reventa | Clasificación falsa | Interpretar booleanos únicamente en campos cuyo nombre expresa obra nueva |
| Valores numéricos de catálogo cambian por portal | Etiqueta incorrecta | No interpretar IDs sin catálogo verificado |

### Orden de implementación

1. [x] `app/domain/models.py`, `app/infrastructure/scrapers/normalization.py` — añadir el
   campo y parser conservador; done cuando señales explícitas se normalizan y señales
   desconocidas quedan en `null`.
2. [x] `app/api/schemas.py`, `app/web/index.html`, `app/web/app.js`, estilos — propagar y
   mostrar la etiqueta; done cuando ambas opciones aparecen y `null` no crea espacio/etiqueta.
3. [x] Tests de dominio/API/UI disponibles — done cuando cubren mapeo, serialización y
   ausencia sin romper datos guardados; suite pertinente en verde.

### Criterios de aceptación

- [x] Un anuncio marcado explícitamente como obra nueva devuelve `listing_condition: "new"`
  y muestra “Obra nueva”.
- [x] Un anuncio marcado explícitamente como segunda mano devuelve
  `listing_condition: "second_hand"` y muestra “Segunda mano”.
- [x] Si el portal no publica el dato, el valor es `null` y no se muestra etiqueta.
- [x] Las búsquedas y resultados antiguos siguen siendo compatibles.

### Checklist de progreso

- [x] 1. Añadir el campo de dominio y normalización conservadora.
- [x] 2. Propagar a API y mostrar la etiqueta en la tarjeta.
- [x] 3. Añadir pruebas focalizadas y validar la suite.

### Evidencia de esta revisión

- Tests nuevos: `app/tests/test_listing_condition.py` y cobertura de paso por Fotocasa en
  `app/tests/test_fotocasa_source.py`.
- `pytest -q`: suite completa en verde.
- `python -m compileall -q app`, `node --check app/web/app.js` y `git diff --check`: en verde.

## Revisión 2026-11-12 — diagnóstico de ejecuciones y resultados obsoletos

### Objetivo

Corregir la idempotencia para que una búsqueda manual no cancele el cron diario y hacer
observable si APScheduler está activo y cuándo ejecutará de nuevo. Si una búsqueda falla
con `503` antes de persistirse, la UI conservará el último resultado correcto en lugar de
vaciarlo, dejando visible que los anuncios mostrados no proceden del intento fallido.

### Alcance

#### Dentro del alcance

- Hacer que la consulta de idempotencia del job diario solo encuentre ejecuciones con
  `trigger="scheduler"`.
- Añadir al endpoint `/health` el estado real del scheduler y la hora de su próxima
  ejecución; mantener `scheduler_enabled` por compatibilidad.
- Conservar los resultados ya mostrados mientras se ejecuta una búsqueda y ante un error.
- Añadir pruebas que reproduzcan los casos anteriores.

#### Fuera del alcance (out of scope)

- Ejecutar o modificar el despliegue remoto, las credenciales o la configuración del
  clúster.
- Reintentar automáticamente búsquedas fallidas o clasificaciones `503`; no se conoce
  todavía si el fallo viene del proveedor LLM, de la red o del Ingress.
- Guardar resultados parciales sin clasificar ni cambiar el contrato de búsqueda.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Idempotencia diaria | Ignorar búsquedas con trigger distinto de `scheduler` | Una ejecución manual de los mismos criterios no debe suprimir el cron. |
| Estado del scheduler | Exponer `scheduler_running` y `next_run_time` desde `/health` | `scheduler_enabled` solo indica configuración y no acredita que el job esté vivo. |
| Fallo durante búsqueda | Mantener el último resultado renderizado y su fecha | El servicio solo persiste búsquedas completas; no presentar una vista vacía como si fuese el resultado nuevo. |

### Contratos de interfaces (ports)

Sin cambios en los métodos públicos de los ports. `ListingRepository.find_by_criteria_and_day`
se precisa como consulta de ejecuciones programadas, no de búsquedas manuales.

### Modelo de datos

Sin cambios en entidades ni datos persistidos. `HealthResponse` añade
`scheduler_running: bool` y `next_run_time: datetime | None`.

### Endpoints de la API

`GET /health` conserva `200` y los campos existentes, añadiendo el estado del scheduler y
la hora ISO-8601 de la próxima ejecución (o `null` si no hay job activo). No se cambian
los endpoints de búsqueda ni sus códigos de error.

### Flujo del job programado

1. Consultar si ya existe un resultado `trigger="scheduler"` para los criterios y fecha.
2. Si existe, registrar el duplicado y terminar; resultados `api` o `profile` no bloquean
   el cron.
3. En otro caso, ejecutar y persistir la búsqueda como hasta ahora.
4. El healthcheck informa si el scheduler está activo y su próxima ejecución; un error
   del proveedor sigue quedando registrado y no se convierte en resultado exitoso.

### Estrategia de logging

Sin cambios. Se mantienen los eventos existentes del job y los campos de contexto; no se
registran credenciales ni datos de contacto.

### Jerarquía de excepciones

Sin cambios. `ClassifierException` continúa respondiendo `503`; una búsqueda que falla
antes de persistirse no reemplaza el último resultado exitoso.

### Plan de tests

- Repositorios JSON y en memoria: una búsqueda `profile` con los mismos criterios/fecha
  no se considera ejecución programada; una búsqueda `scheduler` sí es duplicado.
- Servicio programado: prueba de regresión para la búsqueda manual previa al cron.
- API: `/health` informa scheduler inactivo y ausencia de próxima ejecución cuando está
  deshabilitado, y el estado configurado cuando está activo.
- UI: verificación sintáctica y prueba existente/de navegador si está disponible; no
  borrar la vista anterior al iniciar ni al fallar una búsqueda.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El `503` proviene del proveedor LLM o de la infraestructura externa | No se guarda una búsqueda nueva | Conservar y fechar claramente el último resultado; obtener el `request_id` para investigar logs sin afirmar una causa no verificada. |
| El scheduler está configurado pero su job no se registra | El cron no se ejecuta | Exponer `scheduler_running` y `next_run_time` en `/health`. |
| Cambio accidental en idempotencia | Duplicar el coste de ejecuciones programadas | Seguir omitiendo los resultados `scheduler` del mismo día y cubrirlo con tests de ambos repositorios. |

### Orden de implementación

1. [x] Corregir la consulta de idempotencia — `app/infrastructure/repositories/`, tests — una ejecución `profile` no bloquea el cron y una `scheduler` sí.
2. [x] Exponer estado del job — `app/api/routes.py`, `app/api/schemas.py`, tests — health refleja actividad y próxima ejecución.
3. [x] Conservar la vista previa — `app/web/app.js` — iniciar/fallar una búsqueda no borra los anuncios anteriores.
4. [ ] Ejecutar pruebas focalizadas y comprobaciones — criterios de aceptación basados en resultados reales.

### Criterios de aceptación

- [x] Una búsqueda manual del día no impide la ejecución programada; una ejecución programada previa sí conserva la idempotencia.
- [x] `/health` diferencia scheduler habilitado de scheduler activo y devuelve su próxima ejecución si existe.
- [x] Un intento fallido no se presenta como un nuevo conjunto de resultados ni elimina el último resultado visible.
- [x] Las pruebas focalizadas pasan.

### Checklist de progreso

- [x] 1. Corregir la consulta de idempotencia y cubrirla con pruebas.
- [x] 2. Exponer el estado del scheduler en el healthcheck.
- [x] 3. Conservar los últimos resultados visibles durante búsquedas fallidas.
- [x] 4. Validar las pruebas y comprobaciones acordadas.

### Evidencia de esta revisión

- `pytest -q`: 312 tests pasaron.
- `python -m compileall -q app`, `node --check app/web/app.js` y `git diff --check`: en verde.




## Revisión 2026-11-14 — alertas push de viviendas nuevas en la búsqueda programada

### Objetivo

Avisar en el móvil (iPhone y Android) cuando el job diario de las 07:00 encuentra
viviendas que no aparecían en la ejecución programada anterior. Se envía **un solo
mensaje resumen** por ejecución, para no inundar de notificaciones al usuario.

### Alcance

#### Dentro del alcance

- Puerto `AlertNotifierPort` y adaptador `NtfyNotifier` (ntfy.sh o instancia propia,
  con token opcional para topics protegidos).
- Servicio `listing_alerts`: selección de novedades por `(portal, url)` y construcción
  del digest (máximo 10 viviendas + «…y N más»).
- Consulta `ListingRepository.find_previous_scheduled` en el repositorio JSON y en el
  de memoria, incluida la reconstrucción del índice tras un reinicio.
- Cableado en el contenedor de servicios y en `ScheduledSearchService`.
- Configuración `ALERTS_ENABLED`, `NTFY_BASE_URL`, `NTFY_TOPIC`, `NTFY_TOKEN`,
  `NTFY_PRIORITY` y `ALERT_CLICK_URL`, validada al arrancar.
- Documentación (README, `docs/USAGE.md`, `.env.example`) y manifiestos de Kubernetes
  (ConfigMap y Secret).

#### Fuera del alcance (out of scope)

- Alertas en búsquedas manuales o desde la UI: solo el job programado notifica.
- Avisos de bajada de precio o de anuncios que desaparecen.
- Historial de notificaciones enviadas, reintentos o reenvío de alertas fallidas.
- Cambios en el contrato ni en los códigos de error de los endpoints de búsqueda.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Referencia de comparación | Última ejecución `trigger="scheduler"` del mismo perfil | Una búsqueda manual no representa «lo de ayer»: no debe silenciar novedades ni alterar la idempotencia diaria. |
| Clave de novedad | `(portal, url)` | El mismo enlace en dos portales es una publicación distinta; los identificadores internos pueden cambiar entre ejecuciones. |
| Formato del aviso | Un único digest (10 viviendas + resumen) | Evita una notificación por anuncio y el ruido en el móvil; el detalle completo está en la UI. |
| Primera ejecución | Sin referencia previa todo es nuevo | Si no, la primera mañana tras desplegar el servicio no avisaría de nada. |
| Fallo del notificador | Se registra y no invalida la búsqueda | La búsqueda ya está persistida; la alerta es un extra best-effort. |
| Transporte | ntfy por `POST` HTTP | Apps nativas iOS y Android, sin cuenta ni credenciales de Google/Apple, y permite autoalojarlo. |

### Contratos de interfaces (ports)

- Nuevo `AlertNotifierPort.send_digest(*, title, message, click_url=None)`.
- Nuevo `ListingRepository.find_previous_scheduled(criteria_fingerprint, before)`;
  `None` si el perfil nunca se ha ejecutado de forma programada.
- `ScheduledSearchService` acepta `notifier` y `alert_click_url` opcionales; sin
  notificador el job funciona exactamente igual.

### Modelo de datos

Sin cambios en lo persistido: la referencia son los resultados `trigger="scheduler"`
ya guardados en `data/results`. `ListingDigest` es un valor en memoria.

### Configuración

| Variable | Defecto | Uso |
|---|---|---|
| `ALERTS_ENABLED` | `false` | Activa el envío desde el job diario. |
| `NTFY_BASE_URL` | `https://ntfy.sh` | Servidor ntfy público o propio. |
| `NTFY_TOPIC` | vacío | Topic al que se suscribe la app; obligatorio con alertas activas. |
| `NTFY_TOKEN` | vacío | Token `Bearer`, solo si el topic está protegido. |
| `NTFY_PRIORITY` | `3` | Prioridad mostrada por la app (1–5). |
| `ALERT_CLICK_URL` | vacío | URL que abre la notificación; vacío = primera vivienda nueva. |

Con `ALERTS_ENABLED=true` y sin `NTFY_TOPIC` la aplicación no arranca
(`CONFIGURATION_ERROR`): mejor fallar al desplegar que enviar alertas en silencio a
ningún sitio. El topic se valida contra `[-_A-Za-z0-9]{1,64}`.

### Flujo del job programado

1. Resolver el perfil y comprobar la idempotencia del día (solo ejecuciones `scheduler`).
2. Buscar la última ejecución programada anterior del mismo criterio; un fallo de esta
   consulta se registra y se continúa como si no hubiera referencia.
3. Ejecutar y persistir la búsqueda.
4. Calcular las viviendas nuevas por `(portal, url)`; si no hay ninguna, registrar
   `listing_alert_skipped` y terminar sin enviar nada.
5. Construir el digest y enviarlo con el notificador; un error de envío se registra
   (`listing_alert_failed`) y **no** cambia el resultado devuelto.

### Estrategia de logging

`listing_alert_sent` (con `new_count` y `total_count`), `listing_alert_skipped`,
`listing_alert_failed` (nivel `exception`), `scheduled_search_previous_lookup_failed`,
`alert_delivered` y `alert_delivery_failed`. Nunca se registran `NTFY_TOPIC` ni
`NTFY_TOKEN` completos.

### Jerarquía de excepciones

Nueva `NotificationException` (`NOTIFICATION_FAILURE`, HTTP 502) que lanza el
adaptador ante error de red o respuesta `>= 400`. El job la captura para no tumbar una
búsqueda ya guardada; en la API solo aparecería si se expusiese un endpoint de envío.

### Plan de tests

- `test_listing_alerts.py`: contenido del digest (precio con separador de miles, m²,
  habitaciones, portal, título y URL), singular del título y novedad al cambiar de
  portal con la misma URL.
- `test_ntfy_notifier.py`: endpoint, cuerpo, cabeceras `Title`/`Click`/`Priority`,
  token `Bearer`, errores HTTP y de red, y topic obligatorio.
- `test_scheduled_search.py`: primera ejecución avisa de todo, solo lo ausente de la
  ejecución anterior se envía, silencio cuando no hay novedades, una búsqueda manual no
  es referencia, fallo de ntfy no rompe el job y tope de 10 viviendas.
- Repositorios: `find_previous_scheduled` devuelve la última ejecución anterior,
  ignora las manuales y el propio día, y sobrevive a un reinicio.
- Configuración: alertas desactivadas por defecto, topic obligatorio y validación del
  formato.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| En ntfy.sh cualquier persona que conozca el topic recibe y lee los mensajes | Fuga de datos de la búsqueda | Topic largo y aleatorio, o `NTFY_TOKEN` sobre topic protegido o instancia propia. |
| La app ntfy no está suscrita y nadie ve el aviso | Cambios silenciosos | Suscripción documentada en `docs/USAGE.md`; el fallo de entrega queda en el log. |
| ntfy caído o sin red | Sin notificación | Se registra `listing_alert_failed` y la búsqueda sigue siendo válida y visible en la UI. |
| Un digest gigante se trunca en el móvil | Información perdida | Máximo 10 viviendas y «…y N más»; el resto se consulta en la UI. |
| El topic de la app no coincide con `NTFY_TOPIC` (mayúsculas) | No llega nada y el envío responde `200` | Usar un topic en minúsculas y pegarlo en la app; verificado en `docs/USAGE.md` §10.1. |

### Orden de implementación

1. [x] Puerto, excepción y adaptador ntfy — `app/domain/`, `app/infrastructure/alerts/` — con tests sin red.
2. [x] Selección de novedades y digest — `app/application/services/listing_alerts.py` — con tests de contenido.
3. [x] Consulta de la ejecución anterior en ambos repositorios.
4. [x] Cableado en `ScheduledSearchService` y en el contenedor de servicios.
5. [x] Configuración validada, documentación y manifiestos de despliegue.
6. [x] Pipeline: `ALERTS_ENABLED` activo en el ConfigMap y el topic sincronizado al
   Secret del clúster desde el secreto `NTFY_TOPIC` del repositorio.
7. [x] Ejecutar pruebas y comprobaciones estáticas.

### Criterios de aceptación

- [x] El job diario envía un único resumen con las viviendas ausentes de la ejecución programada anterior.
- [x] Sin ejecución previa avisa de todo lo encontrado; sin novedades no envía nada.
- [x] Una búsqueda manual no cuenta como referencia ni silencia el aviso.
- [x] Un fallo de ntfy se registra y no invalida la búsqueda guardada.
- [x] Con `ALERTS_ENABLED=true` y sin `NTFY_TOPIC` el arranque falla con error de configuración.
- [x] Las pruebas focalizadas y la suite completa pasan.

### Checklist de progreso

- [x] 1. Adaptador ntfy y su puerto.
- [x] 2. Digest de viviendas nuevas.
- [x] 3. Referencia de la ejecución programada anterior.
- [x] 4. Integración en el job diario.
- [x] 5. Configuración, documentación y despliegue.
- [x] 6. Validación final.

### Evidencia de esta revisión

- `pytest -q`: 334 tests pasaron (incluye `test_listing_alerts.py` y `test_ntfy_notifier.py`).
- `python -m compileall -q app`, `node --check app/web/app.js` y `git diff --check`: en verde.
- `GET https://ntfy.sh/v1/health` → `{"healthy":true,"role":"active"}` desde la Pi.
- **Entrega real verificada** el 2026-11-14 con el adaptador de producción y un topic
  privado: `alert_delivered service=ntfy status_code=200`, y el mensaje recuperado del
  servidor con `GET https://ntfy.sh/<topic>/json?poll=1&since=all` (título transliterado a
  ASCII, cuerpo UTF-8 con €, · y m² intactos, `Priority`, `Tags` y `Click` correctos).
  Recibido en el iPhone.
- **Aprendizaje operativo**: los topics de ntfy **distinguen mayúsculas de minúsculas**.
  Un envío a `casa-nueva-AbC` no llega a la suscripción de `casa-nueva-abc`, y no hay
  error (`200` en ambos). Además, en iOS la entrega en segundo plano puede tardar minutos
  aunque el envío haya sido aceptado. Documentado en `docs/USAGE.md` §10.1.
- **Pipeline**: el despliegue sincroniza `NTFY_TOPIC`/`NTFY_TOKEN` desde los secretos del
  repositorio al Secret del clúster con un merge patch (no borra el resto de claves),
  desactiva el paso si `ALERTS_ENABLED` no está a `true`, aborta antes de aplicar los
  manifiestos si falta el topic, y anota el deployment con un hash del topic para
  reiniciar el pod solo cuando cambia. Verificado con `kubectl` simulado en los cuatro
  casos (con topic, con topic y token, sin topic, con alertas desactivadas) y con
  `yaml.safe_load` + `bash -n`.

## Revisión 2026-11-14 — notificaciones por email de viviendas nuevas

### Objetivo

Enviar por correo el mismo resumen de viviendas nuevas que actualmente se envía por ntfy.
El envío de correo será un canal independiente y opcional, sin cambiar la selección de
novedades ni la idempotencia del job.

### Alcance

#### Dentro del alcance

- Adaptador SMTP con STARTTLS, autenticación opcional y varios destinatarios.
- Activación y validación de configuración por variables de entorno.
- Intentar cada canal configurado de forma independiente; un fallo de SMTP no impedirá el
  envío ntfy y viceversa.
- Sincronizar las credenciales de correo del repositorio a Kubernetes durante el pipeline.
- Documentar configuración local y producción.

#### Fuera del alcance (out of scope)

- Elegir o crear una cuenta de correo/SMTP, conseguir credenciales o destinatarios.
- Activar el envío en producción sin que se configuren explícitamente los secretos.
- Enviar correos por búsquedas manuales o añadir un endpoint de prueba.
- Plantillas HTML, adjuntos, proveedores de email específicos o preferencias por usuario.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Transporte | SMTP con STARTTLS; autenticación opcional | Compatible con proveedores comunes y servidores relay sin SDK específico. |
| Contenido | Mismo título/digest de texto plano de ntfy, más enlace de apertura si existe | Una sola construcción del contenido y formato legible en cualquier cliente. |
| Canales | Lista de `AlertNotifierPort`; cada envío se intenta y registra por separado | Fallo aislado por canal y futura extensión sin cambiar el flujo de novedades. |
| Activación | `EMAIL_ALERTS_ENABLED=false` por defecto; exige host, remitente y destinatarios al activarse | No rompe despliegues existentes ni presupone credenciales o dirección de usuario. |
| Secretos de despliegue | Usuario, contraseña, remitente y destinatarios en GitHub Secrets/Kubernetes Secret | No incorporar credenciales ni datos de contacto al repositorio. |

### Contratos de interfaces (ports)

Reutilizar `AlertNotifierPort`; añadir un identificador de canal sin datos sensibles:

```python
class AlertNotifierPort(Protocol):
    channel: str

    async def send_digest(
        self, *, title: str, message: str, click_url: str | None = None
    ) -> None: ...
```

`ScheduledSearchService` recibirá `Sequence[AlertNotifierPort]` y continuará el envío
después de fallos individuales.

### Modelo de datos

No se añaden entidades ni esquemas API. Ajustes de `Settings`: `email_alerts_enabled`,
`email_smtp_host`, `email_smtp_port`, `email_smtp_username`, `email_smtp_password`,
`email_smtp_starttls`, `email_smtp_timeout_seconds`, `email_from` y `email_to`
(destinatarios separados por coma). No se registran direcciones ni credenciales.

### Endpoints de la API

Sin cambios. El correo se envía únicamente como efecto del job programado ya existente.

### Flujo del job programado

1. Seleccionar viviendas nuevas y construir el digest compartido.
2. Intentar cada canal configurado (`ntfy`, `email`) de forma independiente.
3. Registrar el éxito o fallo por canal; conservar el resultado de búsqueda aunque falle
   uno o ambos envíos.

### Estrategia de logging

Mantener `listing_alert_sent` / `listing_alert_failed` con `channel`, conteo y contexto
del job. El adaptador de correo registra `email_alert_delivered` o
`email_alert_delivery_failed` con código y tipo de error, nunca host autenticado,
direcciones, asunto, mensaje ni credenciales.

### Jerarquía de excepciones

Reutilizar `NotificationException` (`NOTIFICATION_FAILURE`) para errores SMTP; el job
captura el fallo por canal para que no invalide la búsqueda ni los otros canales.

### Plan de tests

- `test_email_notifier.py`: destinatarios, asunto/cuerpo/enlace, TLS y autenticación,
  error SMTP y destinatario rechazado mediante un servidor SMTP falso; sin red real.
- `test_config.py`: correo desactivado sin configuración, configuración habilitada
  válida, faltantes y combinaciones incompletas de credenciales.
- `test_scheduled_search.py`: se invocan ambos canales; un canal fallido no bloquea el
  otro ni la búsqueda.
- `test_api.py`/contenedor: composición de canales según configuración.
- Validar pipeline con `bash -n`, manifiestos con parser YAML, suite focalizada y suite
  completa.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| SMTP sin TLS o credenciales mal configuradas | Exposición de datos o correo no entregado | STARTTLS por defecto, validación al arrancar y errores explícitos en logs sin secretos. |
| SMTP caído | Se pierde el correo | Aislar el fallo y continuar con ntfy y la búsqueda guardada. |
| Activar correo antes de guardar sus secretos | El pod no arranca | Mantenerlo desactivado por defecto y validar los secretos en el pipeline antes del despliegue. |
| Una configuración de destinatario contiene varias direcciones | Mensaje duplicado o rechazado | Parsear una lista separada por comas y probar todos los destinatarios. |

### Orden de implementación

1. [x] Generalizar la composición y el envío de canales — `app/domain/ports.py`,
   `app/application/services/scheduled_search.py`, `app/api/dependencies.py` — ambos
   canales se intentan independientemente y conservan la compatibilidad del job.
2. [x] Añadir adaptador SMTP — `app/infrastructure/alerts/email_notifier.py` y
   `app/tests/test_email_notifier.py` — entrega simulada sin red y fallos traducidos a
   `NotificationException`.
3. [x] Añadir configuración validada — `app/config.py`, `app/tests/test_config.py` —
   desactivado por defecto y configuración incompleta rechazada al activarlo.
4. [x] Configurar el despliegue y pipeline — `deploy/k8s/configmap.yaml`,
   `.github/workflows/release.yml`, `docs/INFRASTRUCTURE.md` — correo opt-in, secretos
   sincronizados sin imprimir valores y rollout al cambiar.
5. [x] Documentar uso y validar — `docs/USAGE.md`, tests relevantes — instrucciones para
   SMTP local/producción y comprobaciones en verde.

### Criterios de aceptación

- [x] Una ejecución con novedades envía el digest por cada canal configurado.
- [x] El fallo de un canal no impide intentar los otros canales ni persiste el job como
  fallido.
- [x] Correo desactivado no requiere configuración; habilitarlo sin configuración
  suficiente falla claramente al arrancar/desplegar.
- [x] Los secretos y direcciones no aparecen en logs ni en manifiestos versionados.
- [x] Tests unitarios, suite completa y validaciones del pipeline/manifiestos pasan.

### Checklist de progreso

- [x] 1. Composición de varios canales y envío independiente.
- [x] 2. Adaptador SMTP con tests sin red.
- [x] 3. Configuración de correo y validación.
- [x] 4. Pipeline, Secret y manifiesto opt-in.
- [x] 5. Documentación y validación final.

### Evidencia

- La suite completa de `pytest -q -o addopts=''` pasó (347 tests).
- `compileall`, `git diff --check`, parseo de los YAML de Kubernetes/workflow y `bash -n`
  del paso de sincronización de secretos pasaron.
- El email permanece opt-in (`EMAIL_ALERTS_ENABLED=false`) porque no se han configurado
  un servidor/destinatario ni credenciales SMTP; al habilitarlo, el arranque y el pipeline
  validan la configuración requerida.

## Revisión 2026-11-17 — prueba E2E SMTP con anuncio sintético

### Objetivo

Añadir una prueba real, manual y aislada del envío SMTP que recorra la selección de una
vivienda nueva, la creación del digest y el adaptador de correo. La prueba usará un anuncio
sintético y los secretos SMTP ya configurados en GitHub Actions, sin scraper, DeepSeek ni
la ejecución diaria de producción.

### Alcance

#### Dentro del alcance

- Script de prueba que conecta `ScheduledSearchService` real a un buscador/repositorio
  sintéticos, de modo que un anuncio sintético sea seleccionado como nuevo.
- Envío del digest resultante mediante `EmailNotifier` real y SMTP configurado.
- Workflow manual `workflow_dispatch` para ejecutar la prueba con secretos, sin imprimir
  direcciones ni credenciales.

#### Fuera del alcance (out of scope)

- Añadir o modificar endpoints públicos para enviar correos de prueba.
- Ejecutar Apify, DeepSeek, scrapers o el job diario como parte de esta prueba.
- Cambiar la configuración de producción o enviar alertas de viviendas reales.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Búsqueda | Fake que devuelve exactamente un `SearchResult` sintético | Ejercita el flujo del job sin tráfico ni costes de terceros. |
| Persistencia | Fake sin ejecuciones previas ni ejecuciones del día | Asegura que el anuncio se considera nuevo sin tocar el repositorio de producción. |
| Servicio de notificación | `ScheduledSearchService` y `EmailNotifier` reales | Valida selección, digest, STARTTLS/autenticación y aceptación SMTP. |
| Ejecución | Workflow `workflow_dispatch`, solo con secretos existentes | Permite probar el SMTP real sin exponer secretos en el checkout local. |
| Destinatarios | Secret `EMAIL_TO` configurado | El correo de prueba llega al destino elegido sin añadir direcciones al repo. |

### Contratos de interfaces (ports)

No se modifican los ports de producción. El script implementará dobles mínimos del
`SearchServiceProtocol` y `ListingRepository`, mientras que el notificador real satisface
`AlertNotifierPort`.

### Modelo de datos

Sin cambios de esquema. El script construye un `Listing` marcado claramente como sintético,
lo envuelve en un `ScoredListing` y un `SearchResult` de una sola vivienda.

### Endpoints de la API

Sin cambios. La ejecución se inicia exclusivamente de forma manual en GitHub Actions.

### Flujo del job programado

1. Construir criterios y una vivienda sintética sin acceder a portales.
2. Ejecutar `ScheduledSearchService.run_daily_job()` con dobles de búsqueda/repositorio.
3. Confirmar que el anuncio se selecciona como nuevo y se convierte en digest.
4. Enviar el digest una vez por SMTP real; fallar la ejecución si SMTP rechaza el mensaje.

### Estrategia de logging

Mostrar solo el resultado genérico y el conteo de destinatarios. No registrar direcciones,
host autenticado, asunto, mensaje ni credenciales.

### Jerarquía de excepciones

Reutilizar `NotificationException` del adaptador. Cualquier fallo SMTP termina la acción
con error, en lugar de producir un falso positivo.

### Plan de tests

- Suite local enfocada de `test_scheduled_search.py` y `test_email_notifier.py` para
  verificar el flujo de anuncio nuevo y el aislamiento de fallos con SMTP falso.
- Ejecutar el script de E2E mediante el workflow manual; éxito significa que el servidor
  SMTP aceptó el mensaje. Comprobar la llegada en el buzón configurado.
- Validar sintaxis del workflow y `git diff --check`.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El test envía un correo real | Mensaje de prueba a los destinatarios configurados | Usar asunto y anuncio inequívocamente sintéticos y ejecutar solo manualmente. |
| Secretos ausentes o incorrectos | Fallo de la prueba | Fallar explícitamente por validación o rechazo SMTP; no imprimir valores. |
| El endpoint manual actual ejecuta la búsqueda completa | Coste y alteración de la idempotencia diaria | No usar el endpoint; el workflow solo instancia fakes y el notifier SMTP. |

### Orden de implementación

1. [x] Añadir `scripts/run_email_notification_e2e.py` con servicio/notificadores reales y
   búsqueda/repositorio sintéticos; criterio: un único anuncio sintético llega al port de
   correo y los valores sensibles nunca se imprimen.
2. [x] Añadir `.github/workflows/email-notification-e2e.yml`; criterio: solo es manual y
   proporciona secretos SMTP como variables de entorno sin registrarlos.
3. [x] Ejecutar tests locales y validaciones; criterio: suite focalizada y validadores
   pasan.
4. [x] Ejecutar el workflow manual con los secretos configurados; criterio: SMTP acepta
   el mensaje y el log confirma éxito sin datos personales.

### Criterios de aceptación

- [x] El test sintetiza una única vivienda nueva y recorre `ScheduledSearchService`,
  `build_digest` y `EmailNotifier` reales.
- [x] No se invocan endpoints de búsqueda, scrapers, Apify o DeepSeek.
- [x] La acción manual informa aceptación SMTP sin filtrar secretos/destinatarios.
- [x] La suite enfocada y las validaciones del workflow pasan.

### Checklist de progreso

- [x] 1. Harness E2E de vivienda sintética.
- [x] 2. Workflow manual protegido por secretos.
- [x] 3. Validación local.
- [x] 4. Prueba real SMTP.

### Evidencia

- `pytest -q app/tests/test_scheduled_search.py app/tests/test_email_notifier.py` pasó
  (17 tests); el harness se verificó localmente con el transporte SMTP simulado.
- Workflow `Email notification E2E` (`43985344332`) pasó con una vivienda sintética.
  Los logs registran `email_alert_delivered` para 2 destinatarios y
  `listing_alert_sent` con `new_count=1`, sin mostrar direcciones ni credenciales.
- El servidor SMTP aceptó el mensaje. La llegada final a las bandejas de entrada no se
  puede observar desde este entorno.

## Revisión 2026-11-17 — digest completo en el primer email

### Objetivo

Cuando se habilite el canal SMTP, su primer envío exitoso debe incluir todos los anuncios
clasificados del resultado actual, aunque ya existan búsquedas programadas anteriores a
la activación. Ese email inicial no tendrá el límite habitual de diez entradas. Tras
registrar ese envío, los emails siguientes vuelven a incluir únicamente anuncios nuevos.
El estado debe sobrevivir a reinicios del pod.

### Alcance

#### Dentro del alcance

- Distinguir el primer digest SMTP exitoso del historial de búsquedas.
- Persistir un marcador no sensible junto al almacenamiento duradero de resultados.
- Mantener la selección actual de novedades para ntfy y para los emails posteriores.
- No marcar el primer email como enviado si SMTP falla.
- Documentar el comportamiento y probar persistencia, fallos y selección por canal.

#### Fuera del alcance (out of scope)

- Reenviar todos los anuncios en cada ejecución o en cada reinicio.
- Cambiar la comparación de anuncios nuevos para ntfy.
- Añadir configuración, endpoints o migraciones de base de datos.
- Reenviar el digest completo cuando el marcador ya confirma un envío SMTP exitoso.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Regla de primera entrega | La primera entrega SMTP exitosa envía todos los anuncios de la ejecución actual | Permite activar email aunque ya exista historial de ntfy/búsquedas. |
| Límite del digest inicial | Incluir todas las viviendas clasificadas, sin el límite habitual de diez | Cumple literalmente el requisito de incluir todos los anuncios del primer envío. |
| Estado | Puerto específico de estado de email, con adaptador JSON atómico bajo el volumen persistente | Desacopla el servicio de aplicación y conserva el marcador tras reinicios. |
| Canal push | ntfy sigue recibiendo solo las novedades relativas a la ejecución anterior | El cambio se limita al primer digest de email. |
| Fallo SMTP | No marcar el estado; reintentar el digest completo en la siguiente ejecución | Evita perder el email inicial antes de una aceptación SMTP. |
| Fallo al escribir el marcador después de SMTP | Registrar el fallo explícitamente; permitir un posible reenvío completo | Semántica at-least-once, sin afirmar que el estado quedó persistido. |
| Almacenamiento en memoria | Usarlo solo cuando `RESULTS_DIR` está desactivado | Coincide con el modo de ejecución que tampoco persiste resultados. |

### Contratos de interfaces (ports)

Añadir a `app/domain/ports.py` un `EmailNotificationStatePort` con operaciones asíncronas
para consultar y marcar si ya se aceptó el digest inicial. Implementarlo en infraestructura
con adaptadores JSON persistente y en memoria. No se modifican `AlertNotifierPort` ni
`ListingRepository`.

### Modelo de datos

No cambian entidades ni DTOs. El fichero de estado contendrá únicamente un booleano que
indica si el digest inicial de email fue aceptado; no incluirá direcciones, anuncios ni
credenciales.

### Endpoints de la API

Sin cambios.

### Flujo del job programado

1. Calcular las novedades con la comparación existente.
2. Si el digest inicial de email sigue pendiente, enviar a email todos los anuncios
   clasificados del resultado actual (sin límite de diez) y a ntfy solo las novedades.
3. Marcar el digest inicial como enviado únicamente después de que SMTP retorne éxito.
4. En ejecuciones posteriores, enviar a cada canal solo las novedades.
5. Si no hay anuncios, no enviar ni marcar el digest inicial.

### Estrategia de logging

Registrar consultas/escrituras fallidas del marcador con `search_id`, canal y
`error_code`; no registrar contenido del fichero, direcciones ni anuncios. Mantener el
`new_count` real y distinguir el número enviado cuando el email inicial contiene más
anuncios que los canales push.

### Jerarquía de excepciones

El adaptador de estado traduce errores de lectura/escritura a `RepositoryException`.
El servicio registra esos errores y aplica semántica at-least-once: ante una lectura
fallida trata el digest inicial como pendiente; ante una escritura fallida deja el
marcador sin confirmar. Un fallo SMTP conserva el comportamiento actual por canal.

### Plan de tests

- El primer email incluye todos los anuncios aunque exista una ejecución anterior y el
  conjunto supere diez; ntfy incluye solo los anuncios nuevos.
- Tras el primer envío exitoso, otro email incluye solo nuevas viviendas.
- Un fallo SMTP no marca el digest inicial como enviado.
- El marcador JSON se conserva al reconstruir el adaptador y un fichero inválido da error
  explícito.
- El contenedor elige estado persistente con `RESULTS_DIR` y en memoria sin él.
- Ejecutar tests focalizados, suite completa y `git diff --check`.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El volumen persistente se pierde o se limpia | Se vuelve a enviar un digest completo | El marcador vive bajo el PVC de resultados y su pérdida implica semántica at-least-once. |
| SMTP acepta el correo, pero falla la escritura del marcador | El siguiente job puede duplicar el digest completo | Registrar `email_initial_digest_state_write_failed`; priorizar no perder el primer aviso. |
| Se envía un digest grande en la primera activación | Email extenso | El máximo de resultados ya está acotado; aplicar el límite habitual en notificaciones posteriores. |

### Orden de implementación

1. [x] Añadir el port y adaptadores persistente/en memoria — `app/domain/ports.py`,
   `app/infrastructure/alerts/email_notification_state.py` — leer, marcar y persistir
   solo el estado del digest inicial.
2. [x] Integrar el estado y la selección por canal — `app/application/services/scheduled_search.py`,
   `app/api/dependencies.py` — email inicial completo, ntfy sin cambios y posteriores solo
   con novedades.
3. [x] Añadir pruebas focalizadas — `app/tests/test_scheduled_search.py`,
   `app/tests/test_email_notification_state.py`, `app/tests/test_alert_dependencies.py` —
   comprobar primera entrega, persistencia, fallos y composición.
4. [x] Documentar y validar — `docs/USAGE.md`, `docs/INFRASTRUCTURE.md` — explicar la
   primera entrega y verificar suite/formatos.

### Criterios de aceptación

- [x] El primer email tras activar SMTP contiene todos los anuncios actuales aunque haya
  búsquedas previas.
- [x] ntfy y emails posteriores conservan la semántica de enviar solo novedades.
- [x] El estado solo se confirma tras aceptar SMTP y sobrevive a reinicios con resultados
  persistentes.
- [x] Los fallos del estado se registran sin exponer información sensible.
- [x] Tests focalizados, suite completa y `git diff --check` pasan.

### Checklist de progreso

- [x] 1. Port y almacenamiento duradero del estado inicial.
- [x] 2. Integración de la regla por canal.
- [x] 3. Tests de primera entrega, persistencia y fallo SMTP.
- [x] 4. Documentación y validación.

### Evidencia

- `pytest -q` pasó con toda la suite; `compileall` y `git diff --check` también pasaron.
- Se verificó que el primer digest SMTP no trunca resultados tras diez entradas, que ntfy
  y los emails posteriores solo incluyen novedades y que los fallos SMTP no confirman el
  marcador inicial.

## Revisión 2026-11-19 — imagen principal en alertas por email

### Objetivo

Mejorar el correo de cada vivienda incluida en el digest para mostrar su imagen principal
cuando el anuncio tenga una foto disponible. Mantener el cuerpo de texto como alternativa
accesible y no cambiar el contenido ni el comportamiento de ntfy.

### Alcance

#### Dentro del alcance

- Añadir una alternativa HTML al mensaje del digest, conservando el texto actual.
- Mostrar la primera URL HTTPS de `Listing.photos` como foto principal de cada vivienda
  mostrada en el correo.
- Escapar títulos y URLs al generar HTML y mantener enlaces al anuncio.
- Añadir pruebas focalizadas y documentar que el cliente de correo debe permitir imágenes
  remotas para mostrarlas.

#### Fuera del alcance (out of scope)

- Descargar, transformar o adjuntar imágenes; el correo referenciará el CDN del portal.
- Cambiar los límites actuales de viviendas mostradas, el digest inicial completo, SMTP,
  la selección de novedades o ntfy.
- Añadir secretos, dependencias, endpoints o configuración nueva.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Transporte de imagen | `<img>` con la primera URL HTTPS de `photos` | Evita nuevas solicitudes desde el backend, SSRF, adjuntos voluminosos y dependencia de conversión de imágenes. |
| Formato de correo | Multipart/alternative con texto plano y HTML | Conserva compatibilidad, accesibilidad y el cuerpo de texto ya probado. |
| Separación por canal | El digest conserva mensaje de texto y añade un HTML opcional; ntfy ignora ese campo | La presentación enriquecida es exclusiva del email. |
| Sin foto válida | No mostrar imagen y conservar los detalles/enlace del anuncio | Una foto ausente o URL no HTTPS no debe impedir la alerta. |
| Alcance por digest | Imagen para cada anuncio que ya se muestra como entrada | No altera el límite habitual ni el primer digest completo. |

### Flujo del job programado

1. Construir el digest de texto existente y una representación HTML escapada para las
   mismas viviendas mostradas.
2. Añadir a cada entrada HTML la primera URL HTTPS de `Listing.photos`, si existe.
3. Enviar ambos formatos al canal email; mantener sin cambios los argumentos visibles de
   ntfy y su mensaje de texto.
4. Si el cliente bloquea imágenes remotas, mantener disponible el texto y el enlace del
   anuncio.

### Plan de tests

- El HTML muestra la primera foto, pero no las fotos secundarias, y mantiene texto, precio
  y enlace del anuncio.
- Títulos y URLs con caracteres HTML se escapan; una URL no HTTPS no se usa como imagen.
- El notificador SMTP envía texto y HTML como `multipart/alternative`, y sigue enviando
  solo texto cuando no se proporciona HTML.
- ntfy conserva el contenido de texto sin cambios.
- Ejecutar tests focalizados, suite completa y `git diff --check`.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El cliente bloquea imágenes remotas o el CDN cambia la URL | La foto no se visualiza | El texto, el enlace al anuncio y el cuerpo alternativo permanecen disponibles. |
| Datos del portal se insertan en el HTML | Marcado incorrecto o enlace engañoso | Escapar texto y atributos con las utilidades estándar de HTML y aceptar solo URL HTTPS como `src`. |
| Los anuncios no tienen fotos | No hay imagen para esa vivienda | Mostrar igualmente el resto de datos y el enlace. |

### Orden de implementación

1. Añadir representación HTML segura al digest sin cambiar el mensaje de texto.
2. Extender el contrato de notificación con HTML opcional; consumirlo en SMTP y conservar
   el cuerpo actual de ntfy.
3. Añadir pruebas para el digest, el MIME del correo, ausencia de foto y seguridad de
   escape/protocolo.
4. Documentar la representación de imagen remota y ejecutar validaciones.

### Criterios de aceptación

- [x] Cada entrada mostrada con foto incluye su imagen principal en la versión HTML del email.
- [x] El texto plano sigue disponible y una foto ausente/no HTTPS no bloquea la alerta.
- [x] ntfy y los límites actuales del digest no cambian.
- [x] Tests focalizados, suite completa y `git diff --check` pasan.

### Checklist de progreso

- [x] 1. HTML seguro por vivienda en el digest.
- [x] 2. Integración de la alternativa HTML en el canal email.
- [x] 3. Tests focalizados para imagen, fallback y escape.
- [x] 4. Documentación y validación.

### Evidencia

- Los tests de digest verifican primera imagen HTTPS, escape HTML y fallback sin foto;
  los tests del notificador verifican `multipart/alternative` y que ntfy conserva texto.
- El flujo programado comprueba que la representación HTML llega al email inicial sin
  cambiar el contenido de texto ni el comportamiento de ntfy.
- La suite completa de pytest, `compileall` y `git diff --check` pasaron.

## Revisión 2026-11-20 — resumen conciso por anuncio en email

### Objetivo

Incluir una descripción concisa en cada anuncio mostrado por las alertas de email,
manteniendo la información derivada del anuncio original y sin modificar ntfy.

### Alcance

#### Dentro del alcance

- Tomar `Listing.description`, normalizar espacios y limitar el extracto a 240 caracteres.
- Mostrar el extracto en las versiones de texto y HTML del email, escapándolo en HTML.
- Omitir el extracto si la descripción no existe o está vacía.
- Extender el harness E2E sintético para comprobar el resumen en ambos formatos y enviar
  el mensaje mediante SMTP real.

#### Fuera del alcance (out of scope)

- Generar resúmenes con un LLM, incurrir en más llamadas/coste o añadir nuevas dependencias.
- Cambiar los criterios de novedad, la cantidad de anuncios, ni el contenido de ntfy.
- Reutilizar o persistir resúmenes; cada extracto se calcula desde el resultado actual.

### Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Fuente | `Listing.description` ya obtenido del portal | Evita contenido inventado y llamadas externas adicionales. |
| Resumen | Recortar determinísticamente a 240 caracteres tras colapsar espacios | Es predecible, rápido y suficientemente conciso para email. |
| Canales | Incluirlo solo en el digest construido para email | ntfy conserva su texto y límite actuales. |
| Descripción ausente | No añadir una línea vacía o texto sustituto | Mantiene los correos concisos y usa solo información disponible. |
| Prueba E2E | Anuncio sintético identificable, aserciones de texto/HTML y envío SMTP | Verifica el contenido de la nueva función sin usar un anuncio real. |

### Plan de tests

- El extracto aparece en el texto y HTML del digest email, se compacta el whitespace y
  no supera 240 caracteres.
- Se escapa contenido HTML y se omite una descripción vacía.
- El digest ntfy no contiene la descripción cuando la opción de email no está activada.
- El harness E2E verifica el extracto esperado en ambos formatos antes del envío SMTP y
  informa éxito solo cuando SMTP acepta el mensaje.
- Ejecutar tests focalizados, suite completa, `compileall` y `git diff --check`.
- Ejecutar el E2E SMTP con la configuración de prueba disponible; informar aceptación
  SMTP sin afirmar que se confirmó la llegada final al buzón.

### Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El recorte termina a mitad de una frase | Extracto menos natural | Recortar en el límite de palabra y añadir puntos suspensivos. |
| El texto original contiene marcado HTML | Marcado inválido o inyectado en el email | Escapar el resumen al insertarlo en HTML. |
| SMTP acepta el correo sin entregarlo al buzón | El E2E no prueba llegada final | Reportar únicamente aceptación del servidor SMTP. |

### Orden de implementación

1. Añadir una función determinista de extracto y mostrarla en los dos formatos email.
2. Activar el extracto solo para email desde el job programado.
3. Añadir pruebas focalizadas y adaptar el harness sintético E2E para verificar el contenido.
4. Documentar el comportamiento, ejecutar la suite y realizar el envío E2E solicitado.

### Criterios de aceptación

- Cada anuncio mostrado en email incluye un extracto de su descripción, cuando existe.
- El extracto no excede 240 caracteres, se escapa en HTML y no aparece en ntfy.
- El E2E SMTP verifica el contenido sintético y el servidor acepta el envío.

### Checklist de progreso

- [x] 1. Extracto conciso en texto y HTML exclusivamente para email.
- [x] 2. Cobertura focalizada de formato, límite y fallback.
- [x] 3. Harness E2E verifica el contenido sintético.
- [ ] 4. Validaciones y envío E2E SMTP.
