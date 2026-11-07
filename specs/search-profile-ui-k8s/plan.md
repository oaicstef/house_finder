# Plan: search-profile-ui-k8s

## Objetivo

Convertir el agente en un servicio desplegable en Kubernetes sobre ARM64 que ejecuta una
búsqueda definida por un **perfil JSON configurable** (el mismo cuerpo que usa el job
diario) y publica una **UI web sin build** que lanza esa búsqueda y muestra, por anuncio,
los datos clave y las fotos del portal.

## Alcance

### Dentro del alcance
- Perfil de búsqueda único y configurable (`SEARCH_PROFILE_JSON`) usado por:
  - el job diario (`ScheduledSearchService`),
  - el endpoint de ejecución bajo demanda,
  - la UI (muestra el cuerpo activo y permite relanzarlo).
- Fotos en el dominio: `Listing.photos`, extracción desde `multimedia` de Idealista y
  Fotocasa, persistencia en JSON y exposición en `ListingResponse.photos`.
- API nueva mínima: `GET /api/v1/search-profile` y `POST /api/v1/search-profile/run`.
- UI estática servida por FastAPI (`app/web/`): HTML + CSS + JS vanilla, sin CDN, sin
  build, con tarjetas de anuncio (foto, precio, €/m², m², habitaciones, baños, año,
  ubicación, portal, relevancia, motivo, comodidades y enlace), ordenación por relevancia
  o precio, estados de carga y error.
- Imagen Docker multi-arquitectura (funciona en `linux/arm64`).
- Manifiestos de Kubernetes: Namespace, ConfigMap (perfil incluido), Secret de ejemplo,
  PVC para resultados, Deployment (ARM64, réplica única) y Service ClusterIP.
- Documentación: `README.md`, `docs/USAGE.md`, `deploy/k8s/README.md`, `.env.example`.
- Tests unitarios sin red (perfil, fotos, API, UI estática).

### Fuera del alcance (out of scope)
- Ingress, TLS, cert-manager, HPA y despliegue multi-réplica (el scheduler interno exige
  una sola réplica; se documenta).
- CronJob de Kubernetes: el job sigue dentro del proceso con APScheduler.
- Autenticación de la UI y de la API (despliegue asumido en red privada).
- Paginación, filtros interactivos, favoritos, comparador, notificaciones.
- Almacenamiento de imágenes (se enlazan las URLs del portal, no se copian).
- Cambios en el esquema de salida de los actores más allá de las fotos.
- Frontend con build (React/Vite) o dependencias JS.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Fuente del perfil | `SEARCH_PROFILE_JSON` (string JSON) validado con `SearchCreateRequest` | Un solo cuerpo declarativo, idéntico al de la API, versionable en un ConfigMap. |
| Compatibilidad | Si el perfil está vacío se mantiene `DAILY_*` | No rompe despliegues existentes; el perfil gana cuando está definido. |
| Ejecución bajo demanda | `POST /api/v1/search-profile/run` | La UI no duplica el cuerpo: ejecuta exactamente lo configurado. |
| UI | HTML/CSS/JS estáticos con `StaticFiles` | Sin build ni dependencias nuevas; compatible con ARM y con `pip` puro. |
| Fotos | Se guardan hasta 6 URLs `https` del portal | Coste 0 (no se almacenan binarios); el portal puede bloquear el hotlink y la UI muestra un marcador. |
| Fotos en el LLM | No se envían | No aportan a la clasificación y multiplican tokens. |
| Fotos en persistencia | Sí, hasta 6 por anuncio | Permiten reconstruir la UI desde `GET /results` sin volver a scrapear. |
| Contenedor | `python:3.13-slim`, usuario no root, `uvicorn` | Imagen oficial multi-arquitectura (amd64 + arm64), sin dependencias nativas extra. |
| Réplicas | `replicas: 1` + `strategy: Recreate` | APScheduler dentro del proceso: dos pods duplicarían el job diario. |
| Persistencia | PVC `ReadWriteOnce` en `/data` con `RESULTS_DIR=/data/results` | Los resultados sobreviven al reinicio del pod. |
| Secretos | `Secret` externo al ConfigMap | Tokens fuera de git; el ConfigMap solo lleva configuración no sensible. |

## Contratos de interfaces (ports)

Sin cambios en los ports: `ScraperPort`, `ClassifierPort`, `ListingRepository` y
`PostalCodeLocatorPort` mantienen su firma.

Nuevos contratos de aplicación (no ports):

```python
class Settings:
    def build_search_profile(self) -> SearchCreateRequest | None: ...
    def build_daily_criteria(self) -> SearchCriteria: ...  # perfil si está definido
```

`SearchCriteria`/`Listing` ganan campos; `SearchServiceProtocol` no cambia.

## Modelo de datos

### Dominio (`app/domain/models.py`)

```python
@dataclass(frozen=True, slots=True)
class Listing:
    # ... campos actuales ...
    #: URLs https de fotos del portal (máximo 6, deduplicadas).
    photos: tuple[str, ...] = ()
```

### Esquema Pydantic (`app/api/schemas.py`)

```python
class ListingResponse(BaseModel):
    # ... campos actuales ...
    photos: list[str] = Field(default_factory=list)
```

`SearchCreateRequest` no cambia: el perfil se valida con el mismo modelo.

### Perfil por defecto (`Settings.search_profile_json`)

```json
{
  "location": "Madrid",
  "portals": ["idealista", "fotocasa"],
  "max_price_eur": 900000,
  "min_size_m2": 140,
  "min_rooms": 4,
  "max_results": 50,
  "preferences": "Casa o chalet, padel cominitario, piscina comunitaria",
  "must_have": ["padel"],
  "postal_code": "28031",
  "radius_km": 5,
  "min_year_built": 2020
}
```

## Endpoints de la API

| Método | Ruta | Request | Response | Errores |
|---|---|---|---|---|
| `GET` | `/` | — | `index.html` de la UI | — |
| `GET` | `/static/*` | — | assets de la UI | `404` |
| `GET` | `/api/v1/search-profile` | — | cuerpo del perfil activo | `500` `CONFIGURATION_ERROR` si el perfil es inválido |
| `POST` | `/api/v1/search-profile/run` | — | `SearchResponse` (`201`) | `422` validación, `500` configuración, `502` scraper/geocoder, `503` clasificador |
| `POST` | `/api/v1/searches` | cuerpo libre | `SearchResponse` (`201`) | sin cambios |

`ListingResponse` añade `photos` a los endpoints de búsqueda existentes.

## Flujo del job programado

1. Al arrancar, `Settings` valida `SEARCH_PROFILE_JSON`; un JSON inválido o un campo fuera
   de contrato impide arrancar con `CONFIGURATION_ERROR` y `details.field`.
2. `ScheduledSearchService` recibe los criterios de `build_daily_criteria()`: si hay perfil,
   se usa tal cual; si no, se mantiene la ruta `DAILY_*`.
3. Idempotencia por `criteria_fingerprint` sin cambios: el perfil incluye CP, radio y año,
   por lo que editar el ConfigMap genera una ejecución nueva el mismo día.
4. Réplica única garantizada por el Deployment (`Recreate`, `replicas: 1`).

## Estrategia de logging

| Evento | Nivel | Campos |
|---|---|---|
| `search_profile_loaded` | `INFO` | `source` (`profile`/`legacy`), `location`, `portals`, `postal_code`, `radius_km`, `min_year_built`, `must_have` |
| `search_profile_run_requested` | `INFO` | `search_id`, `trigger="profile"` |
| `search_profile_invalid` | `WARNING` | `error_code="CONFIGURATION_ERROR"`, `field` |
| `listing_photos_extracted` | `DEBUG` | `portal`, `listing_url`, `photos_count` |

Nunca se registran tokens; las URLs de fotos son públicas y no se incluyen en `INFO`
salvo el contador.

## Jerarquía de excepciones

| Clase | `code` | HTTP | Cuándo |
|---|---|---|---|
| `ConfigurationException` | `CONFIGURATION_ERROR` | `500` | `SEARCH_PROFILE_JSON` inválido o incoherente al arrancar/o ejecutar el perfil. |
| `ValidationError` | `VALIDATION_ERROR` | `422` | Perfil válido como JSON pero con combinaciones inválidas detectadas en dominio. |
| `ScraperException` / `ClassifierException` | existentes | `502` / `503` | Sin cambios. |

## Plan de tests

`app/tests/test_search_profile_and_ui.py` (nuevo):
- `Settings`: perfil por defecto válido; `SEARCH_PROFILE_JSON` válido se usa para el job;
  JSON mal formado → `ConfigurationException`; campo desconocido → `ConfigurationException`;
  perfil vacío → se conserva la ruta `DAILY_*`.
- Fingerprint: cambiar `postal_code` o `min_year_built` en el perfil cambia la huella.
- Fotos: extracción desde `multimedia.images[].url` (Idealista) y `multimedia[].url`
  (Fotocasa), deduplicación, filtro de URLs no `https` y tope de 6.
- API: `GET /api/v1/search-profile` devuelve el perfil; `POST /api/v1/search-profile/run`
  ejecuta y devuelve `201` con `photos`; perfil inválido → `500 CONFIGURATION_ERROR`.
- Persistencia: round-trip de `photos` y compatibilidad con JSON antiguos sin el campo.
- UI: `GET /` devuelve HTML con el contenedor de resultados; `/static/app.js` y
  `/static/styles.css` responden `200`.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Hotlink bloqueado por el portal | Tarjetas sin imagen | `onerror` con marcador local; nunca rompe la tarjeta. |
| ConfigMap con JSON mal formado | Pod no arranca | Validación al arrancar con mensaje de campo; `kubectl logs` lo muestra claro. |
| Dos pods ejecutando el cron | Búsquedas duplicadas | `replicas: 1` + `Recreate`; documentado en `deploy/k8s/README.md`. |
| PVC `ReadWriteOnce` en un nodo distinto | Pod pendiente | Documentado; con `ReadWriteOnce` basta una réplica. |
| ARM sin wheel | Fallo de build | Solo dependencias puras o con wheel `manylinux aarch64`; verificado con `pip download --platform`. |
| UI sin autenticación | Exposición pública | Fuera de alcance; se despliega con `ClusterIP` y se accede por `port-forward`. |
| Muchas fotos por anuncio | Payload grande | Tope de 6 URLs por anuncio. |

## Orden de implementación

1. [x] Perfil configurable — `app/config.py`, `.env.example` — criterio: perfil por defecto
   válido y `DAILY_*` como respaldo.
2. [x] Fotos en dominio y scraper — `app/domain/models.py`,
   `app/infrastructure/scrapers/apify_scraper.py` — criterio: ambos portales extraen fotos.
3. [x] Fotos en persistencia y respuesta — `json_file_repository.py`, `api/schemas.py`.
4. [x] Endpoints de perfil y montaje de estáticos — `app/api/routes.py`, `app/main.py`.
5. [x] UI — `app/web/index.html`, `app/web/styles.css`, `app/web/app.js`.
6. [x] Contenedor ARM — `Dockerfile`, `.dockerignore`.
7. [x] Manifiestos k8s — `deploy/k8s/`.
8. [x] Tests — `app/tests/test_search_profile_and_ui.py` + ajustes.
9. [x] Documentación — `README.md`, `docs/USAGE.md`, `deploy/k8s/README.md`.
10. [x] Validación: suite completa, manifiestos renderizados y UI verificada en navegador.

## Criterios de aceptación

- [x] El cuerpo indicado por el usuario es el perfil por defecto y se puede cambiar solo
  con `SEARCH_PROFILE_JSON`, sin tocar código.
- [x] El job diario y `POST /api/v1/search-profile/run` usan ese mismo perfil.
- [x] Cada anuncio devuelve `photos` (máximo 6) y la UI las muestra con fallback.
- [x] La UI lista los datos clave y ordena por relevancia o precio.
- [x] La imagen Docker apunta a `linux/arm64` y el Deployment fija `kubernetes.io/arch: arm64`.
- [x] `kubectl apply -k deploy/k8s` produce Namespace, ConfigMap, PVC, Deployment y Service.
- [x] `pytest` en verde sin llamadas reales.

## Checklist de progreso

- [x] 1. Perfil configurable.
- [x] 2. Fotos en dominio y scraper.
- [x] 3. Fotos en persistencia y respuesta.
- [x] 4. Endpoints de perfil y estáticos.
- [x] 5. UI.
- [x] 6. Contenedor ARM.
- [x] 7. Manifiestos k8s.
- [x] 8. Tests.
- [x] 9. Documentación.
- [x] 10. Validación final.

## Revisión 2026-11-07 — evidencia de validación

1. **Perfil por defecto = cuerpo solicitado.** `Settings.search_profile_json` arranca con
   el JSON del usuario (CP 28031, 5 km, 140 m², 4 hab., 900.000 €, `padel`, 2020) y
   `build_daily_criteria()` lo usa; `SEARCH_PROFILE_JSON=` vacío recupera `DAILY_*`.
   Los tests heredados de `DAILY_*` se marcaron con `search_profile_json=""` explícito.
2. **Ejecución real** (`POST /api/v1/search-profile/run`): `201` en 61 s,
   `scraped=27`, `matched=1`, `trigger=profile`. El anuncio devuelto
   (`idealista/113011260`, chalet pareado, 880.000 €, 151 m², 4 hab., 3 baños) trae
   **6 fotos** y `relevance=0.98`.
3. **Fotos**: Idealista `multimedia.images[].url` y Fotocasa `multimedia[].url`, con tope
   de 6 y deduplicación; se persisten y se devuelven en `ListingResponse.photos`.
4. **UI**: verificada en navegador sobre el servidor real; las tarjetas cargan la imagen
   del portal, los datos clave, las comodidades, el motivo del LLM y el enlace. Si la
   imagen falla se muestra «Sin foto».
5. **Kubernetes**: `kubectl kustomize deploy/k8s` renderiza Namespace, ConfigMap, Service,
   PVC y Deployment; el `SEARCH_PROFILE_JSON` del ConfigMap se parsea como JSON válido y
   el Deployment fija `replicas: 1`, `strategy: Recreate`, `kubernetes.io/arch: arm64`,
   probes `/health`, `fsGroup: 10001` y el PVC en `/data`.
6. **ARM64**: `pip download --platform manylinux_2_28/2_17_aarch64 --python-version 3.13
   --only-binary=:all:` resuelve las 36 dependencias (incluidas `impit`, `pydantic-core`,
   `httptools`, `watchfiles`, `websockets`); `uvloop` (extra de uvicorn) publica wheel
   `cp313 aarch64`. `python:3.13-slim` es multi-arquitectura.
7. **Limitación de la validación**: el daemon de Docker Desktop no estaba en marcha, así
   que `docker buildx build --platform linux/arm64` no pudo ejecutarse aquí; la
   verificación se hizo a nivel de resolución de wheels y de manifiestos.
8. `pytest` → **182 tests** en verde, sin llamadas reales.

