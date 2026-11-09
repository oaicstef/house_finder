# Plan: filtro por tipo de vivienda (piso, adosado, pareado, independiente…)

## Objetivo

Que el usuario final elija qué tipos de vivienda quiere (p. ej. adosados, pareados e
independientes, excluyendo pisos). Idealista admite el filtro `homeType` y lo aplica en
origen; Fotocasa no expone un filtro fiable por subtipo (sus códigos son numéricos), así
que se omite cuando el filtro está activo para no colar pisos.

## Alcance

### Dentro del alcance

- `SearchCriteria.home_types` con la escala canónica del actor de Idealista.
- `Listing.home_type` normalizado (Idealista `detailedType.subTypology` → canónico).
- Envío de `homeType` al actor de Idealista y filtro duro de respaldo.
- Omisión de Fotocasa cuando hay tipos seleccionados (no soporta el filtro).
- API, perfil editable, `DAILY_HOME_TYPES`, UI con casillas y tests.
- Subir `APIFY_MAX_ITEMS` a 100: con el filtro de tipo, tu anuncio queda en el puesto 45
  de 57 pareados/adosados (medido), así que 40 no basta.

### Fuera del alcance

- Mapear los códigos numéricos de Fotocasa.
- Cambiar el resto de filtros o el orden por defecto.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Valores | Los del actor: `flat`, `penthouse`, `duplex`, `terracedHouse`, `semiDetachedHouse`, `detachedHouse`, `villa`, `countryHouse`, `apartment`, `loft` | Se envían tal cual a Idealista y se muestran con etiquetas en español. |
| Alias | `piso`, `adosado`, `pareado`, `independiente`… se normalizan | Perfil y UI toleran lenguaje natural. |
| Fotocasa | Se omite si hay `home_types` | Su `propertySubtype` es numérico y no hay mapeo fiable; mejor 0 que pisos. |
| Desconocido | Se descarta si el filtro está activo | El usuario quiere excluir pisos; un desconocido no puede garantizarse. |
| Tope | `APIFY_MAX_ITEMS=100` | Con el filtro de tipo devuelve 57 y el objetivo está en el 45. |

## Contratos

```python
HOME_TYPES: tuple[str, ...] = (...)
def parse_home_type(value: str) -> str | None: ...
SearchCriteria.home_types: tuple[str, ...] = ()
Listing.home_type: str | None = None
```

## API

`POST /api/v1/searches` acepta `home_types: ["terracedHouse","semiDetachedHouse","detachedHouse"]`.
La respuesta incluye `home_type` por anuncio.

## Tests

- Normalización de alias y valores.
- `homeType` solo para Idealista; Fotocasa omitida con filtro.
- Descarte de tipos no pedidos y de desconocidos con filtro activo.
- API, fingerprint, perfil y `DAILY_HOME_TYPES`.

## Criterios de aceptación

- [x] Con adosados/pareados/independientes, no aparecen pisos.
- [x] El anuncio 111892839 entra con `APIFY_MAX_ITEMS=100`.
- [x] Sin tipos seleccionados, todo funciona como antes.
- [x] Fotocasa se omite solo cuando hay tipos.
