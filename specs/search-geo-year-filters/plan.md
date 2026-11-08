# Plan: search-geo-year-filters

## Objetivo

Permitir búsquedas por radio geográfico (a partir de un código postal o de coordenadas
explícitas) y por año mínimo de construcción, tanto en la API síncrona como en el job
diario. El radio se resuelve a coordenadas con el geocodificador oficial CartoCiudad y se
delega en los actores de Apify; el año se filtra de forma dura cuando el portal lo publica
y se refuerza con el clasificador cuando el dato no está disponible.

Esta feature sustituye el punto «Normalización geográfica avanzada (geocoding), mapas o
cálculo de distancias» que el plan `real-estate-search-agent` declaraba fuera de alcance.

## Alcance

### Dentro del alcance
- Campos nuevos en `POST /api/v1/searches`: `postal_code`, `radius_km`, `latitude`,
  `longitude`, `min_year_built`.
- Resolución `código postal -> Coordinates` con un port de dominio y un adaptador HTTP
  contra CartoCiudad (sin API key), con caché en memoria y timeout configurable.
- Cálculo Haversine en dominio y filtro duro por radio cuando el anuncio publica
  coordenadas; el actor también recibe el radio para acotar en origen.
- Normalización de `latitude`, `longitude` y `year_built` desde Idealista y Fotocasa,
  incluyendo la forma anidada documentada de Fotocasa (`transaction.price`,
  `location.latitude`, `baths`, `street`).
- Filtro duro por `min_year_built` cuando el año consta y refuerzo del criterio en el
  payload/prompt del clasificador LLM cuando no consta.
- Persistencia de los campos nuevos (compatible hacia atrás con JSON ya guardados).
- Configuración del job diario (`DAILY_POSTAL_CODE`, `DAILY_RADIUS_KM`,
  `DAILY_MIN_YEAR_BUILT`) y de geocodificación (`GEOCODING_BASE_URL`,
  `GEOCODING_TIMEOUT_SECONDS`).
- Tests unitarios con fakes de los ports, sin llamadas reales a Apify, DeepSeek ni
  CartoCiudad.
- Actualización de `README.md`, `docs/USAGE.md` y `.env.example`.

### Fuera del alcance (out of scope)
- Geocodificación inversa, mapas, polígonos, barrios o límites administrativos.
- Buscar por código postal sin radio: todo origen geográfico requiere `radius_km`.
- Obtener el año de construcción descargando la ficha HTML de cada anuncio o activando
  `fetchDetails` en el actor de Idealista.
- Sinónimos avanzados de tipo de vivienda (`casa` vs `chalet`) o filtros OR en
  `must_have`.
- Deduplicación entre portales, detección de fraude, reintentos con backoff y circuit
  breaker.
- Autenticación, multi-tenant y cuotas.
- Alta disponibilidad del geocodificador: si CartoCiudad falla, la búsqueda falla con
  error tipado (no hay geocoder de respaldo en esta iteración).

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Origen geográfico | Código postal o par `latitude`/`longitude`; siempre con `radius_km` | Evita ambigüedad («buscar exactamente un CP» no es lo mismo que un radio) y mantiene un único contrato. |
| Geocodificador | CartoCiudad (`https://www.cartociudad.es/geocoder/api/geocoder/find`) | Servicio público oficial español, gratuito, sin API key y con coordenadas de CP verificadas. |
| Cliente HTTP | `urllib.request` de la stdlib dentro de `asyncio.to_thread` | No añade dependencias; el SDK de Apify ya se ejecuta con el mismo patrón. |
| Caché de CP | Diccionario en memoria por instancia del adaptador | Un CP se resuelve una sola vez por proceso; evita latencia y llamadas repetidas. |
| Distancia | Haversine en `Coordinates.distance_km_to` | Fórmula estándar, sin librerías geográficas; suficiente para radios urbanos de 1-50 km. |
| Filtro de radio | El actor acota en origen y el scraper re-filtra con Haversine cuando hay coordenadas | Defensa en profundidad: si el actor devuelve un punto fuera del radio, no llega al LLM. Sin coordenadas en el anuncio se conserva (ya lo filtró el actor). |
| Año de construcción | Duro si el portal lo publica; el LLM lo exige si no consta | No se puede inventar un dato ausente. El LLM recibe `min_year_built` y no debe puntuar como válido lo que no puede confirmar. |
| Normalización Fotocasa | Alias anidados (`transaction.price`, `location.*`, `baths`, `street`) | El adaptador actual espera campos planos y descartaría anuncios válidos de Fotocasa; el radio necesita sus coordenadas. |
| Contrato API | Campos opcionales y `extra="forbid"` se mantiene | Compatibilidad hacia atrás: las peticiones actuales siguen funcionando. |
| Fingerprint | Incluye CP, radio, coordenadas explícitas y año; nunca las coordenadas resueltas | La idempotencia diaria no debe cambiar si el geocodificador devuelve un centroide ligeramente distinto. |
| Resultado persistido | Guarda los criterios originales del usuario, no la copia resuelta | Trazabilidad: se ve qué CP pidió el usuario, no el resultado interno del geocoder. |

## Contratos de interfaces (ports)

`app/domain/ports.py`:

```python
@runtime_checkable
class PostalCodeLocatorPort(Protocol):
    """Resuelve un código postal español en un punto geográfico."""

    async def locate(self, postal_code: str) -> Coordinates | None:
        """Devuelve None si el CP no existe; lanza GeoLocationException si el
        proveedor falla."""
        ...
```

`ScraperPort` no cambia: `SearchService` le pasa una copia resuelta de `SearchCriteria`
con `latitude`/`longitude` ya rellenos, conservando `postal_code` y `radius_km`.

## Modelo de datos

### Dominio (`app/domain/models.py`)

```python
@dataclass(frozen=True, slots=True)
class Coordinates:
    latitude: float
    longitude: float

    def distance_km_to(self, other: "Coordinates") -> float: ...

@dataclass(frozen=True, slots=True)
class Listing:
    # ... campos actuales ...
    year_built: int | None = None
    latitude: float | None = None
    longitude: float | None = None

@dataclass(frozen=True, slots=True)
class SearchCriteria:
    # ... campos actuales ...
    postal_code: str | None = None
    radius_km: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    min_year_built: int | None = None
```

`SearchCriteria.fingerprint()` añade `postal_code`, `radius_km`, `latitude`, `longitude`
y `min_year_built` al payload estable.

### Esquema Pydantic (`app/api/schemas.py`)

```python
postal_code: str | None = Field(default=None, pattern=r"^\d{5}$")
radius_km: int | None = Field(default=None, ge=1, le=50)
latitude: float | None = Field(default=None, ge=-90, le=90)
longitude: float | None = Field(default=None, ge=-180, le=180)
min_year_built: int | None = Field(default=None, ge=1800, le=2100)
```

Validaciones de modelo:
- `latitude` y `longitude` se envían juntas o ninguna.
- `postal_code` y coordenadas explícitas son mutuamente excluyentes.
- `radius_km` exige origen (`postal_code` o coordenadas) y el origen exige `radius_km`.
- `min_year_built <= año actual + 1`.

`ListingResponse` añade `year_built: int | None`.

## Endpoints de la API

### `POST /api/v1/searches`
- Request nuevo (todos opcionales):

```json
{
  "location": "Madrid",
  "portals": ["idealista", "fotocasa"],
  "max_price_eur": 900000,
  "min_size_m2": 150,
  "min_rooms": 4,
  "max_results": 50,
  "preferences": "Casa o chalet, piscina comunitaria, pádel comunitario",
  "must_have": ["padel"],
  "postal_code": "28031",
  "radius_km": 5,
  "min_year_built": 2020
}
```

- Response: igual que hoy, con `listings[].year_built`.
- `201`: búsqueda ejecutada.
- `422`: CP inválido, radio fuera de rango, coordenadas incompletas, combinación
  CP+coordenadas, `min_year_built` fuera de rango o CP no encontrado
  (`code="POSTAL_CODE_NOT_FOUND"`).
- `502`: fallo del geocodificador (`code="GEO_LOCATION_FAILURE"`) o de Apify.

### `GET /api/v1/searches/{search_id}/results`
Sin cambios; los JSON antiguos se leen con valores `None` en los campos nuevos.

### `POST /api/v1/scheduled-searches/run`
Sin cambios de contrato; los criterios del job salen de la configuración.

## Flujo del job programado

1. `Settings.build_daily_criteria()` lee `DAILY_POSTAL_CODE`, `DAILY_RADIUS_KM` y
   `DAILY_MIN_YEAR_BUILT` (vacíos = `None`).
2. Si se define uno de CP/radio sin el otro, se lanza `ConfigurationException` al arrancar.
3. `ScheduledSearchService` calcula el fingerprint con los campos nuevos; la idempotencia
   por fecha sigue siendo válida.
4. `SearchService` resuelve el CP una vez y ejecuta el mismo flujo que la API.
5. Un cambio de CP, radio o año produce un fingerprint distinto y, por tanto, una
   ejecución diaria nueva (comportamiento esperado).

## Estrategia de logging

Eventos nuevos (structlog, con `request_id`/`search_id` del contexto):

| Evento | Nivel | Campos |
|---|---|---|
| `postal_code_resolved` | `INFO` | `postal_code`, `radius_km`, `latitude`, `longitude`, `duration_ms` |
| `geo_radius_applied` | `INFO` | `postal_code`, `radius_km`, `portal`, `kept_count`, `dropped_count`, `unknown_coordinates_count` |
| `listings_filtered_by_radius` | `INFO` | `portal`, `kept_count`, `dropped_count`, `unknown_coordinates_count` |
| `listings_filtered_by_year` | `INFO` | `portal`, `min_year_built`, `kept_count`, `dropped_count`, `unknown_year_count` |
| `postal_code_not_found` | `WARNING` | `postal_code`, `error_code="POSTAL_CODE_NOT_FOUND"` |
| `geo_location_failed` | `WARNING` | `postal_code`, `error_code="GEO_LOCATION_FAILURE"`, `duration_ms` |

Reglas: nunca se registran tokens ni datos de contacto; las coordenadas son de un
inmueble público y el CP es un criterio de búsqueda, no un dato personal del usuario.

## Jerarquía de excepciones

| Clase | `code` | HTTP | Cuándo |
|---|---|---|---|
| `GeoLocationException` (nueva) | `GEO_LOCATION_FAILURE` | `502` | Timeout, error de red o respuesta no procesable de CartoCiudad. |
| `ValidationError` (existente, con `code` override) | `POSTAL_CODE_NOT_FOUND` | `422` | El geocodificador responde sin coordenadas para el CP. |
| `ValidationError` | `VALIDATION_ERROR` | `422` | Combinaciones de radio/coordenadas/CP inválidas o año fuera de rango. |
| `ConfigurationException` | `CONFIGURATION_ERROR` | `500` | No hay `PostalCodeLocatorPort` inyectado y la petición usa `postal_code`; configuración diaria incoherente. |

## Plan de tests

`app/tests/test_geo_and_year_filters.py` (nuevo):
- Esquema API: acepta CP+radio, rechaza CP inválido, radio sin origen, origen sin radio,
  coordenadas incompletas, CP+coordenadas y año fuera de rango.
- `to_domain()` propaga los campos nuevos.
- `SearchService` con `FakePostalCodeLocator`: resuelve el CP, pasa coordenadas al scraper
  y conserva el CP en `SearchResult.criteria`; CP no encontrado -> `ValidationError`;
  locator ausente -> `ConfigurationException`.
- Scraper: el input de ambos portales incluye `latitude`, `longitude`, `distanceKm` y omite
  `location`.
- Haversine: anuncio dentro y fuera de 5 km; sin coordenadas se conserva.
- Año: anuncio con año conocido anterior se descarta; año desconocido se conserva; el
  criterio llega al payload del clasificador.
- Normalización: fixture con forma plana de Idealista (coordenadas y año) y fixture con
  forma anidada de Fotocasa (`transaction.price`, `location.latitude`, `baths`, `street`).
- `CartoCiudadPostalCodeLocator` con `http_get` inyectado: éxito, CP inexistente y error
  HTTP; verifica la URL construida y la caché (una sola llamada para el mismo CP).
- `Settings.build_daily_criteria`: variables vacías -> `None`; CP sin radio ->
  `ConfigurationException`; fingerprint cambia con año/radio/CP.
- Repositorio JSON: round-trip de los campos nuevos y lectura de un JSON antiguo sin ellos.

Tests existentes: se conservan; solo se actualizan los que construyan `Listing` o
`SearchCriteria` con posicionales (hoy todos usan keywords).

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| CartoCiudad no disponible o lento | Búsqueda geo falla | Timeout configurable (`GEOCODING_TIMEOUT_SECONDS`), caché por CP, error tipado 502 y mensaje claro. |
| El centroide del CP no representa todo el área | Un piso a 4,9 km puede quedar fuera por la forma del CP | Se documenta que el radio se mide desde el centroide; el actor también usa un círculo. |
| El actor devuelve un anuncio fuera del radio | Resultado incorrecto | Re-filtro Haversine en el scraper cuando hay coordenadas. |
| Año de construcción ausente en la mayoría de anuncios | Resultados vacíos con `min_year_built` | El LLM lo exige desde la descripción; se registra `unknown_year_count` y se documenta el comportamiento. |
| Cambio del esquema de salida de Fotocasa | Anuncios descartados | Alias múltiples, fixture con la forma documentada y test de normalización. |
| `min_year_built` en el prompt se interpreta como preferencia | Se cuelan casas antiguas | Regla explícita en el system prompt: requisito duro, sin suposiciones. |
| Persistencia con JSON antiguos | `KeyError` al leer | `_deserialize_result` usa `.get()` y valores por defecto. |
| Coste de Apify | Igual que hoy | El radio acota en origen; no se añaden ejecuciones ni `fetchDetails`. |

## Orden de implementación

1. [x] Dominio: `Coordinates`, campos nuevos en `Listing`/`SearchCriteria` y fingerprint —
   `app/domain/models.py` — criterio: los tests de dominio y el resto del proyecto importan
   sin errores.
2. [x] Port y excepción — `app/domain/ports.py`, `app/domain/exceptions.py` — criterio:
   `PostalCodeLocatorPort` tipado y `GeoLocationException` con `code`/`http_status`.
3. [x] Esquema API y validaciones — `app/api/schemas.py` — criterio: peticiones válidas
   pasan y las combinaciones inválidas dan `422`.
4. [x] Resolución en el caso de uso — `app/application/services/search_service.py` —
   criterio: CP resuelto, copia con coordenadas enviada al scraper y criterios originales
   persistidos.
5. [x] Adaptador CartoCiudad y wiring — `app/infrastructure/geocoding/`,
   `app/api/dependencies.py`, `app/config.py` — criterio: locator inyectado en producción y
   configurable por entorno.
6. [x] Scraper: input geográfico, normalización de coordenadas/año/Fotocasa y filtros duros
   — `app/infrastructure/scrapers/apify_scraper.py` — criterio: los fixtures de ambos
   portales se normalizan y los anuncios fuera de radio/año se descartan.
7. [x] Clasificador: payload y prompt con `min_year_built`/geo —
   `app/infrastructure/classifiers/deepseek_classifier.py` — criterio: el criterio duro
   llega al LLM y se refleja en la instrucción.
8. [x] Persistencia — `app/infrastructure/repositories/json_file_repository.py` — criterio:
   round-trip completo y compatibilidad con JSON antiguos.
9. [x] Job diario y `.env.example` — `app/config.py`, `.env.example` — criterio: variables
   documentadas, validadas y con valores vacíos soportados.
10. [x] Tests — `app/tests/test_geo_and_year_filters.py` y ajustes puntuales — criterio:
    suite completa en verde.
11. [x] Documentación — `README.md`, `docs/USAGE.md` — criterio: ejemplo de petición,
    tabla de campos y límites del año/geocodificador documentados.

## Criterios de aceptación

- [x] Una petición con `postal_code: "28031"`, `radius_km: 5` y `min_year_built: 2020`
  devuelve `201` y el input de Apify contiene las coordenadas del CP y `distanceKm: 5`.
- [x] Un anuncio con coordenadas a más de 5 km del centroide se descarta antes del LLM.
- [x] Un anuncio con `year_built` conocido anterior a 2020 se descarta; el LLM recibe
  `min_year_built` y la instrucción de no asumir años desconocidos.
- [x] Las combinaciones inválidas (radio sin origen, CP+coordenadas, año fuera de rango)
  devuelven `422` con el formato de error uniforme.
- [x] El job diario admite `DAILY_POSTAL_CODE`, `DAILY_RADIUS_KM` y
  `DAILY_MIN_YEAR_BUILT`, y su fingerprint cambia al modificarlos.
- [x] Los resultados guardados antes de la feature se siguen leyendo.
- [x] `pytest` pasa sin llamadas reales a Apify, DeepSeek ni CartoCiudad.

## Checklist de progreso

- [x] 1. Dominio: `Coordinates` y campos nuevos.
- [x] 2. Port `PostalCodeLocatorPort` y `GeoLocationException`.
- [x] 3. Esquema API y validaciones.
- [x] 4. Resolución de CP en `SearchService`.
- [x] 5. Adaptador CartoCiudad, settings y wiring.
- [x] 6. Scraper: input geográfico, normalización y filtros.
- [x] 7. Clasificador: payload y prompt.
- [x] 8. Persistencia compatible.
- [x] 9. Job diario y `.env.example`.
- [x] 10. Tests y suite en verde.
- [x] 11. Documentación (`README.md`, `docs/USAGE.md`).

## Revisión 2026-11-07 — hallazgos al validar los actores reales

La implementación se contrastó con el dataset real de ambos actores y con una ejecución
de radio de 2 anuncios por portal (coste acotado con `max_total_charge_usd`):

1. **Fotocasa publica `antiquity` en años**, no el año de construcción. Se añadió
   `_ANTIQUITY_ALIASES` y `year_built = año_actual - antiquity` (solo si el valor está
   entre 0 y 200). Ejemplo real: `antiquity=7` → `year_built=2019`.
2. **Fotocasa sí expone `url`** y un mapa `location` con `latitude`/`longitude` y nombres
   (`level5Name`, `level7Name`, `level8Name`). La ubicación normalizada compone
   `calle número, barrio, distrito, municipio`, lo que da más contexto al LLM.
3. **Ambas ejecuciones reales aceptaron `latitude`/`longitude`/`distanceKm`** y el
   scraper registró `listings_filtered_by_radius` / `listings_filtered_by_year` con los
   contadores esperados.
4. **Los tests no tocan red**: 157 tests, todos en verde. La validación real se hizo con
   scripts puntuales fuera de la suite.
5. **El adaptador CartoCiudad** se verificó contra el endpoint público real para `28031`
   (centroide `40.37876642147255, -3.602295005978209`).

## Revisión 2026-11-07 (b) — `SCRAPER_FAILURE` por escalones de Idealista

Un usuario recibió `502 SCRAPER_FAILURE` («Fallo al scrapear el portal idealista») al probar
la feature con `min_size_m2: 90`. Reproducción con el cliente real:

```
InvalidRequestError: Input is not valid: Field input.minSize must be equal to one of the
allowed values: "0", "60", "80", "100", "120", "140", "160", "180", "200", "220", "240",
"260", "280", "300"
```

No se creaba ningún run de Apify: el 400 ocurre al crear la ejecución, por eso no aparecía
en el histórico de runs. El esquema del actor también limita `minPrice`/`maxPrice` a
escalones (500-3000 y 50000-4000000).

1. **`_PortalProfile` gana `price_steps`/`size_steps`** con los escalones leídos del
   `inputSchema` real de cada build (Idealista); Fotocasa los deja en `None`.
2. **`_snap_to_steps`** ajusta siempre al lado permisivo (mínimos hacia abajo, máximos
   hacia arriba) y usa `0` («sin límite») si el valor queda fuera del rango del actor. El
   valor exacto del usuario se sigue aplicando en `_matches_hard_criteria`, así que el
   resultado no cambia, solo el prefiltro del portal.
3. **`scraper_input_snapped` (DEBUG)** deja rastro del valor pedido y del enviado.
4. Tests nuevos: ajuste de escalones, valores fuera de rango y ausencia de ajuste en
   Fotocasa. `Docs`: tabla de esquemas por portal y caso de error 502.

Evidencia: `pytest` → 161 tests en verde. Reproducción real del payload que fallaba
(`postal_code=28031`, `radius_km=5`, `min_size_m2=90`): run `NhGWIaXhizlVP9IHE` SUCCEEDED,
`maxItems=40`, 40 anuncios extraídos y 24 conservados tras el filtro de radio, todos con
≥90 m² y ≤600.000 €.

