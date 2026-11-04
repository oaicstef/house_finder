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
| Scraping | `apify-client` ejecutado con `asyncio.to_thread` | Apify abstrae proxies/anti-bot; el cliente es síncrono, se aísla en un hilo para no bloquear el event loop. IDs de actor configurables (`APIFY_IDEALISTA_ACTOR`, `APIFY_FOTOCASA_ACTOR`) para no acoplar el código a un actor concreto. |
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




