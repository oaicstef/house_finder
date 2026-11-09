# Plan: ordenar por distancia

## Objetivo

Que el usuario final pueda ordenar los resultados por distancia al origen de la búsqueda
(código postal o lat/lon). La API expone `distance_km` por anuncio y la UI añade la opción
"Distancia (más cerca primero)" en el selector de orden. Sin origen geográfico, la opción
cae a relevancia.

## Alcance

### Dentro del alcance

- Persistir el **origen resuelto** (CP → coordenadas) en el resultado, sin tocar los
  criterios ni la huella de idempotencia.
- `ListingResponse.distance_km` calculado con Haversine (redondeo a 2 decimales).
- Opción de orden en la UI y distancia visible en la tarjeta.
- Compatibilidad con resultados antiguos (sin origen → `distance_km: null`).
- Tests de API, servicio, persistencia y UI.

### Fuera del alcance (out of scope)

- Ordenar por distancia en el servidor o cambiar el orden por defecto (relevancia).
- Geocodificar la dirección de cada anuncio (solo se usan sus coordenadas del portal).
- Cambiar los filtros de radio existentes.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Dónde guardar el origen | `SearchResult.origin_latitude/longitude` | Es un dato del resultado; así no se altera `criteria` ni el fingerprint. |
| Cálculo | En la respuesta (`ListingResponse`), vía `Coordinates.distance_km_to` | Reutiliza la Haversine del dominio y no toca el clasificador ni el scraper. |
| Orden | En el navegador (selector existente) | Ya existe el selector; no hace falta un parámetro nuevo en la API. |
| Sin coordenadas | `distance_km: null` y el comparador los deja al final | Evita esconder anuncios sin geolocalización. |
| Sin origen | `distance_km: null` para todos | Coherente con criterios sin CP/lat-lon. |

## Contratos de interfaces (ports)

```python
@dataclass(frozen=True, slots=True)
class SearchResult:
    ...
    origin_latitude: float | None = None
    origin_longitude: float | None = None

class ListingResponse(BaseModel):
    distance_km: float | None
```

## Modelo de datos

- `SearchResult` gana el origen resuelto (opcional, al final, con default).
- JSON persistido: `"origin": {"latitude": ..., "longitude": ...}`; los ficheros antiguos
  sin esa clave se leen con `None`.
- `ListingResponse.distance_km: float | None` (km, 2 decimales).

## Endpoints de la API

`GET /api/v1/searches/{id}/results` y `/searches/latest` incluyen en cada anuncio:

```json
{"url": "...", "distance_km": 3.24, ...}
```

## Flujo del job programado

1. `run_search` resuelve el CP una sola vez (`_resolve_geo`) y pasa los criterios resueltos
   al scraper.
2. El resultado guarda `criteria` original + `origin_*` resueltos.
3. La respuesta calcula la distancia de cada anuncio.
4. La UI ordena con `distance_km` ascendente; los `null` van al final.

## Estrategia de logging

- `search_origin_resolved` (debug) con `latitude`/`longitude` (ya existe
  `postal_code_resolved` en INFO).
- Sin cambios en logs de anuncios.

## Jerarquía de excepciones

Sin excepciones nuevas: si el CP no se resuelve se mantiene `ValidationError`
(`POSTAL_CODE_NOT_FOUND`).

## Plan de tests

- Distancia correcta con origen y coordenadas del anuncio.
- `null` sin origen o sin coordenadas del anuncio.
- `run_search` guarda el origen (lat/lon directas y CP con locator fake).
- Round-trip del repositorio JSON con y sin `origin` (compatibilidad).
- UI: opción `distance` y `distance_km` en JS; versión de assets v=4.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Anuncios sin coordenadas | No se pueden ordenar | Quedan al final (`Infinity`). |
| Ficheros antiguos sin `origin` | Error de lectura | `.get("origin") or {}`. |
| CP con centroide poco preciso | Distancia aproximada | Misma aproximación que el filtro de radio; se documenta. |

## Orden de implementación

1. [ ] Dominio: origen en `SearchResult`.
2. [ ] Servicio: resolver una vez y guardar el origen.
3. [ ] Persistencia JSON.
4. [ ] API: `distance_km`.
5. [ ] UI: opción de orden, distancia en la tarjeta y cache-busting.
6. [ ] Tests y documentación.

## Criterios de aceptación

- [x] Con origen, cada anuncio devuelve `distance_km`.
- [x] Ordenar por "Distancia" deja primero el más cercano y al final los `null`.
- [x] Resultados antiguos siguen funcionando con `distance_km: null`.
- [x] La suite completa sigue en verde.

## Checklist de progreso

- [x] 1. Dominio.
- [x] 2. Servicio.
- [x] 3. Persistencia.
- [x] 4. API.
- [x] 5. UI.
- [x] 6. Tests y docs.
