# Plan: criterios editables desde la UI y últimos resultados siempre visibles

## Objetivo

Que el usuario final pueda cambiar el perfil de búsqueda preconfigurado desde la UI (sin
editar `SEARCH_PROFILE_JSON` ni reiniciar el pod) y que, al abrir la UI desde cualquier
dispositivo, se muestren siempre los resultados de la última búsqueda guardada, no solo los
que viva en la memoria del navegador que los lanzó.

## Alcance

### Dentro del alcance

- Puerto `SearchProfileStore` (persistencia del perfil efectivo) y su adaptador en fichero
  JSON junto a las búsquedas guardadas.
- Servicio de aplicación `SearchProfileService`: resolver el perfil efectivo (override del
  usuario > `SEARCH_PROFILE_JSON` > `DAILY_*`), guardarlo y restaurarlo.
- Endpoints `PUT`/`DELETE /api/v1/search-profile` y `GET /api/v1/searches/latest`.
- `POST /api/v1/search-profile/run` ejecuta el perfil **efectivo** (override incluido).
- El job diario usa el mismo perfil efectivo (se resuelve al inicio de cada ejecución).
- UI: formulario de criterios (guardar/restaurar) y carga de la última búsqueda al abrir.
- Tests unitarios y de API con los fakes existentes (sin Apify ni DeepSeek).
- Documentación: `README.md`, `docs/USAGE.md`, `deploy/k8s/README.md` y este plan.

### Fuera del alcance (out of scope)

- Autenticación o multiusuario: hay un único perfil compartido (igual que hoy).
- Historial navegable de búsquedas en la UI (solo la última).
- Edición concurrente con control de versiones (el último `PUT` gana).
- Cambiar `SEARCH_PROFILE_JSON` del ConfigMap desde la API (sigue siendo el valor por
  defecto de referencia).
- Notificaciones, favoritos o comparador.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Persistencia del perfil | Fichero `<directorio padre de RESULTS_DIR>/search-profile.json` (`/data/search-profile.json` en k8s) | El PVC de `/data` ya es escribible por UID 10001 y sobrevive a los rollouts; no añade estado en memoria del pod. |
| Tipo del puerto | `SearchCriteria` (dominio), no JSON | El adaptador traduce a/desde disco; el servicio trabaja con entidades y no conoce Pydantic. |
| Precedencia | override guardado > `SEARCH_PROFILE_JSON` > `DAILY_*` | Mantiene compatibilidad y deja `SEARCH_PROFILE_JSON` como "valor de fábrica" restaurable. |
| Restaurar | `DELETE /search-profile` borra el override y devuelve el perfil de configuración | Revertir no requiere reiniciar el pod ni editar el ConfigMap. |
| Job diario | `ScheduledSearchService` acepta un `criteria_provider` opcional | La UI y el cron deben usar el mismo criterio; sin provider se conserva el comportamiento actual (tests existentes). |
| Validación | `validate_criteria` extraída a `app/domain/validation.py` | La comparten `SearchService` y `SearchProfileService` sin duplicar reglas. |
| Últimos resultados | `ListingRepository.find_latest()` + `GET /searches/latest` | El repositorio ya persiste cada búsqueda; solo falta exponer la más reciente. |
| Cache del navegador | `app.js`/`styles.css` se enlazan con `?v=2` | Evita que un móvil con la versión anterior en caché no vea la nueva UI. |

## Contratos de interfaces (ports)

```python
@runtime_checkable
class SearchProfileStore(Protocol):
    async def get(self) -> SearchCriteria | None: ...
    async def save(self, criteria: SearchCriteria) -> None: ...
    async def clear(self) -> None: ...


@runtime_checkable
class ListingRepository(Protocol):
    # ... métodos existentes ...
    async def find_latest(self) -> SearchResult | None: ...


class SearchServiceProtocol(Protocol):
    # ... métodos existentes ...
    async def get_latest(self) -> SearchResult: ...
```

## Modelo de datos

- No hay entidades nuevas. El perfil persistido es la serialización estable de
  `SearchCriteria` (mismos campos que `POST /api/v1/searches`).
- `SearchProfileResponse.source` pasa a `override | profile | legacy`.

## Endpoints de la API

| Método | Ruta | Request | Response | Errores |
|---|---|---|---|---|
| GET | `/api/v1/search-profile` | — | `200 {source, profile}` | 500 `CONFIGURATION_ERROR` si el perfil de configuración es inválido |
| PUT | `/api/v1/search-profile` | `SearchCreateRequest` | `200 {source:"override", profile}` | 422 validación, 500 `REPOSITORY_FAILURE` |
| DELETE | `/api/v1/search-profile` | — | `200 {source, profile}` | 500 `CONFIGURATION_ERROR` |
| POST | `/api/v1/search-profile/run` | — | `201 SearchResponse` | los mismos que `POST /searches` |
| GET | `/api/v1/searches/latest` | — | `200 SearchResponse` | 404 `SEARCH_NOT_FOUND` si no hay ninguna |

## Flujo del job programado

1. Al disparar el job (APScheduler o `POST /scheduled-searches/run`) se resuelve el perfil
   efectivo con `SearchProfileService.resolve_criteria()`.
2. Se calcula el `fingerprint` de ese criterio y se aplica la idempotencia por fecha.
3. Si ya hay ejecución para ese fingerprint y hoy, se omite (`executed=false`).
4. El resultado se persiste como siempre, por lo que pasa a ser el "último" de la UI.

## Estrategia de logging

| Evento | Nivel | Campos |
|---|---|---|
| `search_profile_updated` | INFO | `source`, `criteria_fingerprint`, `portals` |
| `search_profile_reset` | INFO | `source`, `criteria_fingerprint` |
| `search_profile_store_failed` | ERROR | `error_code`, `path` |
| `search_profile_store_skipped_file` | WARNING | `error_code`, `file` |
| `latest_search_served` | INFO | `search_id`, `created_at`, `duration_ms` |
| `latest_search_missing` | WARNING | `error_code` |

## Jerarquía de excepciones

| Clase | `code` | HTTP |
|---|---|---|
| `ValidationError` (existente) | `VALIDATION_ERROR`/`INVALID_POSTAL_CODE` | 422 |
| `ConfigurationException` (existente) | `CONFIGURATION_ERROR` | 500 |
| `RepositoryException` (existente) | `REPOSITORY_FAILURE` | 503 |
| `SearchNotFoundError` (existente) | `SEARCH_NOT_FOUND` | 404 |

## Plan de tests

- **Fakes**: `InMemorySearchProfileStore` con `FakeScraper`/`FakeClassifier`, sin red.
- **API**: `GET/PUT/DELETE /search-profile`, `POST /search-profile/run` con override
  (el fake de scraper recibe el criterio guardado) y `GET /searches/latest` (404 y 200).
- **Repositorio**: `JsonSearchProfileStore` con `tmp_path` (guardar, leer, borrar, fichero
  corrupto → `None` + WARNING) y `find_latest` en los dos repositorios.
- **Job diario**: el provider cambia el criterio usado y el fingerprint de idempotencia.
- **UI estática**: `index.html` contiene los ids del formulario; `app.js` hace `PUT` y
  `GET /searches/latest`.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Fichero de perfil corrupto | La app arrancaría sin perfil | `get()` devuelve `None` y se usa el de configuración; se registra `WARNING`. |
| `PUT` con criterios que el scraper rechaza | Búsqueda fallida al ejecutar | Se valida con `SearchCreateRequest` (422) y `validate_criteria` antes de guardar. |
| Dos pestañas guardando a la vez | Gana el último `PUT` | Aceptado (documentado); el perfil es único. |
| PVC de solo lectura o sin permisos | `PUT` falla | `RepositoryException` → 503 con mensaje; el perfil de configuración sigue funcionando. |
| Móvil con `app.js` antiguo en caché | No ve el formulario | Query `?v=2` en `index.html`. |
| El job diario ignora el override | Criterio distinto entre UI y cron | Provider inyectado y test específico. |

## Orden de implementación

1. [x] `app/domain/validation.py` + `SearchService` delegando — validación compartida — tests actuales en verde.
2. [x] `app/domain/ports.py` — `SearchProfileStore`, `find_latest`, `get_latest` — imports tipados sin ciclos.
3. [x] `app/infrastructure/repositories/search_profile_store.py` — adaptadores JSON y memoria — tests de fichero.
4. [x] Repositorios de resultados — `find_latest` — tests de repositorio.
5. [x] `app/application/services/search_profile.py` — servicio de perfil — tests unitarios.
6. [x] `ScheduledSearchService` — `criteria_provider` — test de idempotencia con provider.
7. [x] Contenedor y `Settings.search_profile_file` — wiring — `build_container` compone el servicio.
8. [x] `app/api/schemas.py` y `routes.py` — endpoints nuevos — tests de API.
9. [x] `app/web/index.html`, `app.js`, `styles.css` — formulario y carga del último resultado — UI verificada.
10. [x] Documentación y revisión del plan — README/USAGE/deploy — secciones actualizadas.

## Criterios de aceptación

- [x] `PUT /api/v1/search-profile` guarda el perfil y `POST /search-profile/run` usa ese perfil.
- [x] `DELETE /api/v1/search-profile` restaura el perfil de configuración.
- [x] El job diario usa el perfil efectivo (override incluido).
- [x] `GET /api/v1/searches/latest` devuelve la búsqueda más reciente y 404 si no hay ninguna.
- [x] Al abrir la UI (p. ej. en móvil) se ven los resultados de la última búsqueda guardada.
- [x] La UI permite editar, guardar y restaurar los criterios.
- [x] `pytest` en verde sin llamadas reales (**211 tests**).

## Checklist de progreso

- [x] 1. Validación compartida.
- [x] 2. Puertos.
- [x] 3. Almacén del perfil.
- [x] 4. `find_latest` en repositorios.
- [x] 5. Servicio de perfil.
- [x] 6. Provider del job diario.
- [x] 7. Wiring del contenedor.
- [x] 8. Endpoints.
- [x] 9. UI.
- [x] 10. Documentación y validación.

## Revisión 2026-11-08 — evidencia de validación

1. **Perfil editable**: `PUT` escribe `search-profile.json` junto a los resultados y
   `GET` devuelve `source: "override"`; `DELETE` lo borra y vuelve a `source: "profile"`.
   Verificado con el contenedor real (`build_container`) sobre un directorio temporal:
   el fichero aparece en disco con `postal_code: 46001` y desaparece al restaurar.
2. **Job diario**: `ScheduledSearchService` resuelve el criterio con
   `criteria_provider` en cada ejecución; un test comprueba que el cron usa el override
   guardado por la UI (`location=Valencia`).
3. **Última búsqueda**: `GET /api/v1/searches/latest` devuelve el mismo `search_id` y los
   mismos anuncios que la ejecución previa, y `404 SEARCH_NOT_FOUND` mientras no haya
   ninguna. Un test simula dos "navegadores" (clientes) contra el mismo contenedor.
4. **UI**: `index.html` incluye el formulario (`criteria-form`, `save-criteria`,
   `reset-criteria`) y `app.js` hace `PUT /search-profile` y `GET /searches/latest` al
   cargar; `node --check` limpio y `?v=2` para invalidar la caché del móvil.
5. `pytest` → **211 tests** en verde, sin Apify ni DeepSeek.
