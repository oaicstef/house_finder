# Plan: filtro por eficiencia energética (mínimo B)

## Objetivo

Permitir exigir una calificación energética mínima (p. ej. **B**, es decir, A o B) en las
búsquedas. El filtro se aplica como criterio duro cuando el portal publica la etiqueta y,
cuando no consta, el clasificador LLM la valora a partir de la descripción. Hoy la
calificación no se ingiere en absoluto, así que la feature cubre captura, modelo, filtro,
API, perfil y UI.

## Alcance

### Dentro del alcance

- Nuevo campo `Listing.energy_rating` (letra A-G) extraído del payload de Apify.
- Nuevo criterio `SearchCriteria.max_energy_rating` ("B" = B o mejor).
- Filtro duro en el scraper con contadores y log `listings_filtered_by_energy`.
- `fetchDetails` en el actor de Idealista **solo** cuando se pide el filtro.
- Contrato API (request/response), perfil editable, `DAILY_*`, UI y clasificador.
- Tests unitarios y de API.

### Fuera del alcance (out of scope)

- Filtrar en el propio actor (no existe input de eficiencia en los actores).
- Normalizar la calificación de Fotocasa más allá de alias defensivos (su actor no
  documenta el campo; si no viene, queda como desconocida).
- Mostrar la etiqueta en la tarjeta de la UI (solo se expone en la respuesta JSON).

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Semántica | `max_energy_rating` con "B" = A o B | "B como mínimo" significa B o mejor; A queda incluida. |
| Escala | `("A","B","C","D","E","F","G")` en dominio | Comparación por índice; A es la mejor. |
| Dato desconocido | Se conserva y lo evalúa el LLM | Igual que `min_year_built`: descartar por desconocido vaciaría resultados. |
| Origen del dato | `energyCertification.energyConsumption.rating` (Idealista) y alias defensivos | Es la forma usada por los scrapers públicos de Idealista. |
| Coste | `fetchDetails: true` solo si el filtro está activo y el portal lo soporta | El actor lo documenta como 1 request extra por inmueble (~50× más lento). |
| Flags por portal | `supports_fetch_details` en `_PortalProfile` | Evita enviar claves no soportadas a Fotocasa (su actor valida el input). |

## Contratos de interfaces (ports)

No cambian los ports (`ScraperPort`, `ClassifierPort`, `ListingRepository`). El cambio es
de datos:

```python
@dataclass(frozen=True, slots=True)
class Listing:
    ...
    energy_rating: str | None = None   # "A".."G"

@dataclass(frozen=True, slots=True)
class SearchCriteria:
    ...
    max_energy_rating: str | None = None   # "B" = B o mejor

ENERGY_RATING_SCALE: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G")
```

## Modelo de datos

- `Listing.energy_rating: str | None` (mayúscula, una letra).
- `SearchCriteria.max_energy_rating: str | None`.
- `SearchCreateRequest.max_energy_rating: Literal["A".."G"] | None` (normaliza
  minúsculas/prefijos con un validador `before`).
- `ListingResponse.energy_rating: str | None`.
- `fingerprint()` incluye el criterio (idempotencia del cron).
- Persistencia: `search-profile.json` y resultados guardados incluyen el campo.

## Endpoints de la API

`POST /api/v1/searches` y `POST /api/v1/search-profile/run` aceptan:

```json
{"location": "Madrid", "max_energy_rating": "B", "postal_code": "28031", "radius_km": 5}
```

Errores: `422` si el valor no está en A-G (vía `Literal`) o si el dominio recibe otro
valor (`ValidationError` → `VALIDATION_ERROR`).

`GET /api/v1/search-profile` devuelve `max_energy_rating` y cada anuncio incluye
`energy_rating`.

## Flujo del job programado

1. `SEARCH_PROFILE_JSON` (o `DAILY_MAX_ENERGY_RATING`) define el criterio.
2. El scraper pide `fetchDetails` a Idealista y normaliza `energy_rating`.
3. Si la etiqueta consta y es peor que la mínima → se descarta (`energy_dropped`).
4. Si no consta → se conserva (`energy_unknown`) y el LLM lo evalúa.
5. La huella incluye el criterio: cambiar de "sin filtro" a "B" genera una ejecución
   nueva el mismo día.

## Estrategia de logging

- `listing_energy_unknown` (debug) por anuncio sin etiqueta.
- `listings_filtered_by_energy` (info) por portal con `max_energy_rating`, `kept_count`,
  `dropped_count`, `unknown_rating_count`.
- `scraper_fetch_details_enabled` (debug) al activar el modo detallado.

## Jerarquía de excepciones

| Caso | Excepción | HTTP |
|---|---|---|
| Valor fuera de A-G en el DTO | `RequestValidationError` (Pydantic) | 422 |
| Valor inválido en dominio/perfil | `ValidationError` | `VALIDATION_ERROR` |
| `DAILY_MAX_ENERGY_RATING` inválido | `ConfigurationException` | arranque |

## Plan de tests

- Extracción desde `energyCertification.energyConsumption.rating` y desde `_details`.
- Normalización de `"b"`, `"Clase B"`, `"B - 45 kWh/m²"`.
- Descartar D cuando el mínimo es B; mantener A/B; mantener desconocidas.
- `fetchDetails` solo con filtro y solo en Idealista.
- Fingerprint distinto con y sin criterio.
- API: acepta `"b"` → `"B"`, rechaza `"H"`, respuesta incluye `energy_rating`.
- Config: `daily_max_energy_rating` llega a `SearchCriteria` y `""` → `None`.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| `fetchDetails` multiplica el coste/tiempo | Timeout del actor | Solo se activa con el filtro; documentar `APIFY_TIMEOUT_SECONDS` y `APIFY_MAX_ITEMS`. |
| Fotocasa no publica la etiqueta | Filtro poco efectivo en ese portal | Alias defensivos + el LLM decide con la descripción. |
| Anuncios sin certificado | Resultados vacíos si se descartaran | Se conservan como desconocidos. |
| `"certificate"` empieza por C | Falso positivo al parsear texto libre | Regex estricta `^(?:clase\s*)?([A-G])\b`. |

## Orden de implementación

1. [ ] Dominio: `ENERGY_RATING_SCALE`, `Listing.energy_rating`, `SearchCriteria.max_energy_rating`, fingerprint.
2. [ ] Scraper: extracción, `fetchDetails` condicional, filtro y logs.
3. [ ] Validación y esquemas API.
4. [ ] Config (`DAILY_*` y perfil por defecto).
5. [ ] Persistencia (perfil y resultados).
6. [ ] Clasificador (prompt y payload).
7. [ ] UI (select, render, fill, read).
8. [ ] Tests y documentación.

## Criterios de aceptación

- [x] `POST /api/v1/searches` con `"max_energy_rating": "B"` descarta anuncios con D o peor.
- [x] Un anuncio con A o B se mantiene; uno sin etiqueta se mantiene y llega al LLM.
- [x] `fetchDetails` solo aparece en el input del actor de Idealista cuando el filtro está activo.
- [x] El perfil guardado y la respuesta JSON conservan `max_energy_rating`/`energy_rating`.
- [x] La suite completa sigue en verde.

## Checklist de progreso

- [x] 1. Dominio.
- [x] 2. Scraper.
- [x] 3. API.
- [x] 4. Config.
- [x] 5. Persistencia.
- [x] 6. Clasificador.
- [x] 7. UI.
- [x] 8. Tests y docs.
