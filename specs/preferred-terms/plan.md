# Plan: términos preferentes (opcionales) destacados y ordenados primero

## Objetivo

Permitir marcar términos deseables pero **no excluyentes** (p. ej. `padel`): los anuncios
que los mencionan se mantienen aunque no los tengan, se marcan en la respuesta y la UI, y
se ordenan por delante del resto. Complementa `must_have` (obligatorio) y `preferences`
(texto libre para el LLM).

## Alcance

### Dentro del alcance

- `SearchCriteria.preferred_terms: tuple[str, ...]` (vacío = sin cambio de comportamiento).
- Detección sobre `searchable_text` (título, descripción y comodidades), sin filtro duro.
- `matched_terms` en cada anuncio de la respuesta y distintivo en la tarjeta.
- Orden: primero los que cumplen al menos un término preferente; después relevancia y precio.
- Perfil editable, `SEARCH_PROFILE_JSON`, `DAILY_PREFERRED_TERMS`, UI y fingerprint.
- Tests de detección, orden, API, perfil y configuración.

### Fuera del alcance (out of scope)

- Cambiar el prompt del LLM o la puntuación de relevancia.
- Detección semántica (solo coincidencia de texto normalizado, igual que `must_have`).
- Reordenar cuando el usuario elige otro criterio en el selector de orden.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Nombre | `preferred_terms` / `matched_terms` | Paralelo a `must_have`; deja claro que no excluye. |
| Detección | `Listing.matches_any(terms)` sobre `searchable_text` | Reutiliza la normalización existente (acentos, mayúsculas). |
| Orden | `(0 si destaca, -relevancia, precio)` en `SearchService` | Garantiza "los que lo tienen, primero" sin tocar el LLM. |
| Persistencia | Se guarda el criterio; `matched_terms` se calcula al responder | Evita duplicar estado en los resultados guardados. |
| UI | Campo "Destacar (opcional)" + insignia ★ en la tarjeta | Visible sin abrir el anuncio. |
| Orden en UI | La opción "Relevancia" antepone los destacados | Evita que el reordenado del navegador pierda la prioridad. |

## Contratos de interfaces (ports)

```python
def Listing.matches_any(self, terms: Sequence[str]) -> tuple[str, ...]:
    """Términos preferentes presentes en el anuncio (vacío si ninguno)."""

@dataclass(frozen=True, slots=True)
class SearchCriteria:
    preferred_terms: tuple[str, ...] = ()
```

## Modelo de datos

- `SearchCreateRequest.preferred_terms: list[str]` (máx. 8, 2-40 caracteres, deduplicado).
- `ListingResponse.matched_terms: list[str]`.
- `fingerprint()` incluye los términos normalizados y ordenados.
- Perfil y resultados persistidos incluyen `preferred_terms`.

## Endpoints de la API

```json
{"location": "Madrid", "preferred_terms": ["padel"], "must_have": ["piscina"]}
```

`GET /api/v1/searches/latest` devuelve cada anuncio con `"matched_terms": ["padel"]`
cuando corresponda. Sin `preferred_terms` el comportamiento es idéntico al actual.

## Flujo del job programado

1. El perfil fija `preferred_terms` (o `DAILY_PREFERRED_TERMS` con la ruta legacy).
2. El scraper y el LLM no cambian: no se descarta nada por estos términos.
3. Tras clasificar, se ordena por destacados → relevancia → precio y se recorta a
   `max_results`.
4. La huella cambia al añadir o quitar términos, evitando reutilizar la ejecución diaria.

## Estrategia de logging

- `listings_ranked_by_preferences` (info) con `preferred_terms`, `highlighted_count` y
  `matched_count`.

## Jerarquía de excepciones

| Caso | Excepción | HTTP |
|---|---|---|
| Término de menos de 2 o más de 40 caracteres | `RequestValidationError` | 422 |
| Más de 8 términos | `RequestValidationError` | 422 |

## Plan de tests

- `padel` en la descripción → `matched_terms == ("padel",)`; sin mención → `()`.
- Un anuncio sin el término **no** se descarta.
- El orden pone primero los que lo tienen aunque tengan menos relevancia.
- API: normaliza/deduplica, expone `matched_terms`, fingerprint distinto.
- Config: `DAILY_PREFERRED_TERMS` llega a los criterios.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Falsos positivos ("padel" en una zona, no en la vivienda) | Destacado incorrecto | Es solo un destacado, no un filtro; se muestra el término para revisarlo. |
| Muchos términos preferentes | Ruido en la UI | Límite de 8 y deduplicado. |
| El navegador reordena y pierde la prioridad | El usuario no ve primero los destacados | El comparador por defecto antepone `matched_terms`. |

## Orden de implementación

1. [ ] Dominio: `matches_any`, `preferred_terms`, fingerprint.
2. [ ] API: campo, validador, respuesta y `matched_terms`.
3. [ ] Servicio: orden por destacados.
4. [ ] Persistencia y config (`DAILY_PREFERRED_TERMS`).
5. [ ] UI: campo, insignia y orden.
6. [ ] Tests, docs y plan de validación.

## Criterios de aceptación

- [x] Con `preferred_terms: ["padel"]`, ningún anuncio se descarta por no tenerlo.
- [x] Los anuncios que lo mencionan aparecen primero y con `matched_terms: ["padel"]`.
- [x] La tarjeta muestra una insignia ★ con el término.
- [x] Sin `preferred_terms` la respuesta y el orden no cambian.
- [x] La suite completa sigue en verde.

## Checklist de progreso

- [x] 1. Dominio.
- [x] 2. API.
- [x] 3. Servicio.
- [x] 4. Persistencia y config.
- [x] 5. UI.
- [x] 6. Tests y docs.

## Revisión 2026-11-08 (verificación)

Implementado y cubierto con 8 tests nuevos (`app/tests/test_preferred_terms.py`): detección
con acentos, no exclusión, orden por destacados aunque la relevancia sea menor, exposición
en `matched_terms`, normalización/deduplicado en la API, fingerprint y `DAILY_PREFERRED_TERMS`.
Suite completa: 239 tests en verde. Perfil de producción: `must_have: ["piscina"]` y
`preferred_terms: ["padel"]`.
