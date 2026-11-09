# Guía de uso

Guía práctica para poner en marcha y consumir el agente de búsqueda inmobiliaria.
Para el diseño interno consulta [../specs/real-estate-search-agent/plan.md](../specs/real-estate-search-agent/plan.md);
para los filtros de radio y año, [../specs/search-geo-year-filters/plan.md](../specs/search-geo-year-filters/plan.md);
para el resumen del proyecto, [../README.md](../README.md).

## Índice

1. [Requisitos](#1-requisitos)
2. [Puesta en marcha](#2-puesta-en-marcha)
3. [Obtener los tokens](#3-obtener-los-tokens)
4. [Configurar los actores de Apify](#4-configurar-los-actores-de-apify) — incluye [qué papel juega Apify](#41-qué-papel-juega-apify), [¿necesito suscripción?](#42-necesito-una-suscripción-de-pago), [entorno `.env`](#43-entorno-env) y [esquema por portal](#44-esquema-de-entrada-por-portal)
5. [Arrancar el servidor](#5-arrancar-el-servidor)
6. [Recetas con curl](#6-recetas-con-curl)
7. [Cliente en Python](#7-cliente-en-python)
8. [Interpretar la respuesta](#8-interpretar-la-respuesta)
9. [Errores y cómo resolverlos](#9-errores-y-cómo-resolverlos)
10. [Búsqueda diaria programada](#10-búsqueda-diaria-programada)
11. [Perfil de búsqueda configurable y UI](#11-perfil-de-búsqueda-configurable-y-ui)
12. [Despliegue en Kubernetes (ARM64)](#12-despliegue-en-kubernetes-arm64)
13. [Modo sin credenciales (tests)](#13-modo-sin-credenciales-tests)

---

## 1. Requisitos

- Python **3.11+** (probado con 3.13).
- Cuenta de **Apify** con un actor de scraping por portal.
- Clave de API de **DeepSeek**.
- Opcional: `curl` o `httpx` para probar la API.

## 2. Puesta en marcha

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

```bash
# Linux / macOS
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Edita `.env` y rellena como mínimo:

```dotenv
APIFY_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxx
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
```

## 3. Obtener los tokens

**Apify**

1. Entra en <https://console.apify.com/account/integrations>.
2. Copia el *Personal API token* en `APIFY_TOKEN`.

**DeepSeek**

1. Entra en <https://platform.deepseek.com/api_keys>.
2. Crea una clave y cópiala en `DEEPSEEK_API_KEY`.

> Las claves nunca se registran en los logs. Si faltan, el arranque falla con
> `CONFIGURATION_ERROR` y un mensaje que lista las variables ausentes.

## 4. Configurar los actores de Apify

### 4.1 ¿Qué papel juega Apify?

Idealista y Fotocasa **no ofrecen API pública** y aplican protección anti-bot (bloqueo por
IP de datacenter, rate limiting, retos de JavaScript). En vez de implementar y mantener
navegadores headless y rotación de proxies, se delega en **actores de Apify**: programas ya
empaquetados que hacen ese trabajo y publican el resultado como un *dataset* JSON.

En la arquitectura, Apify es **una implementación más del puerto `ScraperPort`**:

```
SearchService → ScraperPort (interfaz) → ApifyListingScraper → Apify
```

Secuencia real para cada portal ([`apify_scraper.py`](../app/infrastructure/scrapers/apify_scraper.py)):

1. `client.actor(actor_id).call(run_input, timeout_secs)` — lanza el actor y espera a que termine.
2. `run["defaultDatasetId"]` — localiza el dataset de resultados.
3. `client.dataset(id).iterate_items()` — recorre los anuncios crudos.
4. `_normalize()` — traduce cada item a la entidad de dominio `Listing`.
5. `_matches_hard_criteria()` — descarta lo que incumple precio/m²/habitaciones **antes**
   de llamar al LLM, para no gastar tokens.

Al ser el SDK síncrono, la llamada se ejecuta con `asyncio.to_thread` para no bloquear el
event loop de FastAPI.

**Lo que Apify NO hace:** no filtra por preferencias en lenguaje natural ni puntúa
relevancia. Solo devuelve anuncios crudos; eso lo resuelve el clasificador DeepSeek.

**Coste:** cada ejecución de actor consume créditos. `APIFY_MAX_ITEMS` acota el gasto y la
idempotencia del job diario evita repetir búsquedas ya hechas. Sin token o con un actor
inexistente, la API responde `502` con `code: "SCRAPER_FAILURE"`.

**Sustituible:** al estar detrás de un `Protocol`, cambiar de proveedor (o usar un scraper
propio) es escribir otra clase con `fetch_listings()` y cambiarla en `build_container()`.
Ni los servicios ni los endpoints se tocan.

### 4.2 ¿Necesito una suscripción de pago?

**No para empezar.** Apify tiene un plan **Free** ($0/mes) que incluye **$5 de uso** al mes,
suficiente para probar y para un uso ligero. Necesitas **crear una cuenta** (gratis) para
obtener el API token, pero no una suscripción de pago.

Hay dos costes independientes que conviene no confundir:

| Coste | Quién lo cobra | Notas |
|---|---|---|
| **Plataforma** (compute, proxies, almacenamiento) | Apify | El plan Free incluye $5/mes. Si lo agotas, **el acceso se bloquea** hasta el mes siguiente (no se factura de más). |
| **Actor de Store** | El autor del actor | La mayoría son *pay-per-event* (p. ej. por cada resultado). Algunos son *pay-per-usage* y otros gratuitos. |

Lo que determina tu factura real es el **actor concreto de Idealista/Fotocasa** que elijas,
no el plan. Revisa la pestaña **Pricing** de cada actor en Apify Store antes de usarlo: verás
si cobra por resultado, por ejecución o solo el uso de plataforma.

Planes (precios de la página oficial de Apify, nov. 2025):

| Plan | Precio | Uso incluido | Notas |
|---|---|---|---|
| Free | $0 | $5/mes | 5 ejecuciones concurrentes, 16 GB RAM |
| Starter | $19/mes | $19/mes | Descuentos Bronze en Store, soporte por chat |
| Scale | $199/mes | $199/mes | Descuentos Silver |
| Business | $999/mes | $999/mes | Descuentos Gold, account manager |

> Los créditos **no se acumulan** entre meses. En planes de pago, el exceso se factura
> *pay-as-you-go* hasta el límite de gasto que configures.

**Controlar el gasto en este proyecto:**

```dotenv
APIFY_MAX_ITEMS=40          # menos anuncios = menos eventos facturables
APIFY_MAX_CHARGE_USD=0.50   # tope duro por ejecución de actor (opcional)
```
`APIFY_MAX_CHARGE_USD` se pasa al SDK como `max_total_charge_usd`: el actor se detiene al
alcanzarlo y **nunca se factura por encima de ese límite**. Además, el job diario es
idempotente, así que no repite ejecuciones (ni gasto) el mismo día con los mismos criterios.

Recomendación: empieza con el plan **Free** y un `APIFY_MAX_ITEMS` bajo; si el uso diario te
resulta útil, pasa a **Starter ($19/mes)**.

### 4.3 Entorno `.env`

Configuración mínima que funciona (verificada en vivo):

```dotenv
APIFY_TOKEN=apify_api_...
DEEPSEEK_API_KEY=sk-...
SEARCH_OPERATION=sale

# Actores (salvo que uses los tuyos, deja estos)
APIFY_IDEALISTA_ACTOR=igolaizola/idealista-scraper
APIFY_FOTOCASA_ACTOR=igolaizola/fotocasa-scraper

APIFY_MAX_ITEMS=40
APIFY_TIMEOUT_SECONDS=600
APIFY_MAX_CHARGE_USD=0.15

# Geocodificador de códigos postales (radio geográfico)
GEOCODING_BASE_URL=https://www.cartociudad.es/geocoder/api/geocoder
GEOCODING_TIMEOUT_SECONDS=10

RELEVANCE_THRESHOLD=0.5
```

`APIFY_TIMEOUT_SECONDS=600` evita cortar el run: con 40 anuncios el actor tarda ~25 s, pero
`fetchDetails`/`fetchStats` lo hacen mucho más lento.

### 4.4 Esquema de entrada por portal

Cada actor tiene su propio vocabulario y el scraper lo adapta automáticamente (perfiles
`_PROFILES` en [`apify_scraper.py`](../app/infrastructure/scrapers/apify_scraper.py)):

| Concepto | `igolaizola/idealista-scraper` | `igolaizola/fotocasa-scraper` |
|---|---|---|
| `operation` | `sale` \| `rent` | `buy` \| `rent` |
| `propertyType` | `homes` | `home` |
| `country` | `es` (obligatorio) | no aplica |
| `minPrice`/`maxPrice`/`minSize` | **string** y **escalones cerrados** | **entero** y valor libre |
| Búsqueda por radio | `latitude`/`longitude` + `distanceKm` | `latitude`/`longitude` + `distanceKm` |

> **Idealista rechaza valores intermedios.** Su esquema solo admite
> `minSize`/`maxSize` ∈ {0, 60, 80, 100, …, 300} y precios en escalones
> (`500`–`3000` y `50000`–`4000000`). Cualquier otro valor devuelve
> `400 InvalidRequestError: Field input.minSize must be equal to one of the allowed values`.
> El scraper ajusta automáticamente al escalón **permisivo** (mínimos hacia abajo,
> máximos hacia arriba) y deja el valor exacto para el filtro duro local; el ajuste se
> registra como `scraper_input_snapped` (DEBUG). Fotocasa no necesita ajuste.

### 4.4.1 Esquema de salida por portal

El normalizador lee cada campo con varios alias y los perfiles aportan los constructores
que faltan, así que **los dos actores verificados funcionan sin tocar código**:

| Concepto | `igolaizola/idealista-scraper` | `igolaizola/fotocasa-scraper` |
|---|---|---|
| Enlace | `url` | `url` (ruta `/es/comprar/.../<id>/d`) |
| Precio | `price` (string `"625000"`) | **`transaction.price`** (entero) |
| Superficie | `size` | `surface` |
| Habitaciones / baños | `rooms` / `bathrooms` | `rooms` / **`baths`** |
| Título | campo propio o `suggestedTexts.title` | **no existe**: se compone de `street` + `number` + niveles de `location` |
| Ubicación | `address` (texto) | `location.level8Name`/`level7Name`/`level5Name` (objeto) |
| Comodidades | `features.hasSwimmingPool`, … | `features` solo trae ids (`{"id": "17"}`); se leen del **slug** (`…-jardin-piscina/190094194/d`) |
| Año de construcción | `yearBuilt`/`constructionYear` o la descripción | `antiquity` (antigüedad en años) o la descripción |
| Fotos | `multimedia.images[].url` | `multimedia[].url` (máximo 6) |
| Coordenadas | `latitude`/`longitude` planos | `location.latitude`/`location.longitude` |

> **Fotocasa** está validado en vivo (build `zQ6P2E7A5hwEubfcJ`): los 3 anuncios de una
> ejecución se normalizaron con precio, superficie, baños y enlace. Viene desactivado por
> defecto porque es **pay-per-event**: cada ejecución cuesta unos céntimos. Actívalo con
> `APIFY_FOTOCASA_ACTOR=igolaizola/fotocasa-scraper`.
>
> Las comodidades de Fotocasa se deducen del slug de la URL, reutilizando los sinónimos de
> `AMENITY_LABELS` (`swimming_pool` → piscina, `garden` → jardín, …). Así `must_have`
> funciona igual en los dos portales.

### 4.4.2 Entrada común

Ambos comparten `location`, `maxItems` y `minPrice`/`maxPrice`/`minSize`. Cuando hay
origen geográfico, el scraper omite `location` y envía `latitude`, `longitude` y
`distanceKm`. Los portales **sin actor configurado se omiten** con un `WARNING`
(`scraper_portal_skipped`) en lugar de tumbar la búsqueda; si ninguno está configurado, se
lanza `ScraperException`. Si un actor responde con 0 anuncios se registra
`scraper_portal_returned_no_listings` con la ubicación resuelta, para que el portal no
desaparezca en silencio de los resultados.

### 4.5 Elegir los actores

Los valores por defecto **son identificadores reales**: `igolaizola/idealista-scraper`
activo y `igolaizola/fotocasa-scraper` vacío (portal omitido). No son placeholders: un id
inexistente devuelve `404` y tumba la búsqueda completa.

1. En Apify Store busca un actor de Idealista y otro de Fotocasa.
2. Abre el actor y copia su identificador, con formato `usuario/nombre-del-actor`.
3. Pégalos en `.env`:
```dotenv
APIFY_IDEALISTA_ACTOR=igolaizola/idealista-scraper
APIFY_FOTOCASA_ACTOR=igolaizola/fotocasa-scraper   # vacío = no usar Fotocasa
SEARCH_OPERATION=rent        # rent (alquiler) | sale (compraventa)
APIFY_MAX_ITEMS=40           # tope por portal
APIFY_TIMEOUT_SECONDS=300
```

El scraper envía al actor este input (`location` o `latitude`/`longitude` + `distanceKm`,
`operation`, `maxItems` y, si están definidos, `minPrice`, `maxPrice`, `minSize`). Si tu
actor usa otros nombres de campo, ajústalo en `_build_run_input` en
[`app/infrastructure/scrapers/apify_scraper.py`](../app/infrastructure/scrapers/apify_scraper.py).

El normalizador reconoce varias claves por campo (`url`/`link`/`detailUrl`,
`price`/`priceEur`/`priceAmount`, `size`/`squareMeters`/`surface`, ...), además de las
coordenadas planas o anidadas y del año de construcción
(`yearBuilt`/`constructionYear`/... o «construido en 2021» en la descripción).

## 5. Arrancar el servidor

```bash
uvicorn app.main:app --reload
```

| Recurso | URL |
|---|---|
| Swagger UI | <http://127.0.0.1:8000/docs> |
| ReDoc | <http://127.0.0.1:8000/redoc> |
| OpenAPI JSON | <http://127.0.0.1:8000/openapi.json> |
| Health check | <http://127.0.0.1:8000/health> |

Comprueba que está vivo:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","scheduler_enabled":true}
```

## 6. Recetas con curl

### 6.0 Caso práctico: chalet con piscina y pádel

**Si el pádel es imprescindible, no lo pongas solo en `preferences`: usa `must_have`.**
`preferences` es una sugerencia para el LLM (afecta a la puntuación); `must_have` es un
filtro **duro** que descarta el anuncio si no lo menciona.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/searches \
  -H "Content-Type: application/json" \
  -d '{
    "location": "Madrid",
    "portals": ["idealista"],
    "max_price_eur": 900000,
    "min_size_m2": 200,
    "min_rooms": 4,
    "max_results": 5,
    "preferences": "Chalet, padel, piscina",
    "must_have": ["padel", "piscina"]
  }'
```

Resultado real (`scraped=30, matched=2`):

```json
[
  {
    "price_eur": 897000.0,
    "size_m2": 287.0,
    "rooms": 5,
    "title": "Chalet adosado en Calle Sous de Plata, Pau de Carabanchel, Madrid",
    "url": "https://www.idealista.com/inmueble/105237601/",
    "relevance": 1.0,
    "reason": "... y tiene piscina y pádel verificados."
  }
]
```

**Cómo se comprueba `must_have`** ([`Listing.satisfies_all`](../app/domain/models.py)):

- Busca cada término en **título + descripción + comodidades etiquetadas**.
- Ignora acentos y mayúsculas: `"padel"` encuentra `"pista pádel"`.
- Las comodidades del portal cuentan con sinónimos: `swimming_pool` → `piscina`,
  `garden` → `jardín`, `lift` → `ascensor`, etc.
- Semántica **AND**: deben cumplirse **todos** los términos.

| Necesitas | Dónde ponerlo |
|---|---|
| Precio, m², habitaciones | Campos dedicados (`max_price_eur`, `min_size_m2`, `min_rooms`) |
| Piscina, jardín, terraza, ascensor, trastero | `must_have` (o el campo dedicado, si existe) |
| Pádel u otros extras del texto libre | `must_have` |
| Gustos que solo deben puntuar | `preferences` |

> **El pádel es raro.** De ~30 candidatos en Madrid solo 1-2 lo mencionan. Por eso:
> sube `APIFY_MAX_ITEMS` (100 o más) para tener más candidatos, y recuerda que
> `max_results` solo recorta la respuesta final, no lo que se busca.

Referencia de la escala que se le pide al modelo:

| `relevance` | Significado |
|---|---|
| 0.9-1.0 | Cumple el filtro y todas las preferencias |
| 0.7-0.9 | Cumple el filtro y la mayoría de preferencias |
| 0.5-0.7 | Cumple el filtro pero le faltan varios extras |
| 0.3-0.5 | Cumple el filtro pero es otro tipo de vivienda |
| 0.0-0.3 | Incumple el filtro o no es lo que se busca |

Las `amenities` (`swimming_pool`, `garden`, `terrace`, `air_conditioning`, `lift`,
`storage_room`) provienen de los flags del portal y se envían al LLM como datos
verificados: si pides piscina y no aparece `swimming_pool`, el modelo sabe que no debe
suponerla.

### 6.1 Radio geográfico y año de construcción

Para buscar a menos de 5 km del código postal **28031** (Villa de Vallecas, Madrid) y
exigir construcción desde **2020**:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/searches \
  -H "Content-Type: application/json" \
  -d '{
    "location": "Madrid",
    "portals": ["idealista", "fotocasa"],
    "max_price_eur": 900000,
    "min_size_m2": 150,
    "min_rooms": 4,
    "max_results": 50,
    "preferences": "Casa o chalet, piscina comunitaria, pádel comunitario",
    "must_have": ["piscina"],
    "preferred_terms": ["padel"],
    "postal_code": "28031",
    "radius_km": 5,
    "min_year_built": 2020,
    "max_energy_rating": "B"
  }'
```

- **`postal_code` + `radius_km`**: el CP se resuelve con [CartoCiudad](https://www.cartociudad.es/)
  (servicio público del IGN, sin API key) a su centroide. Los actores reciben
  `latitude`, `longitude` y `distanceKm`; `location` se usa solo como etiqueta.
- **Alternativa sin CP**: `"latitude": 40.378766, "longitude": -3.602295, "radius_km": 5`.
  No se puede combinar `postal_code` con `latitude`/`longitude`.
- **`preferred_terms`**: deseables pero **no excluyentes** (p. ej. `padel`). Se buscan en
  título, descripción y comodidades; los anuncios que los mencionan vuelven en
  `matched_terms` y se ordenan por delante del resto, sin descartar a los demás.
- **`min_year_built`**: filtro duro cuando el portal publica el año (`year_built` en la
  respuesta). Si no consta, el LLM exige que la descripción lo confirme; los anuncios sin
  año pueden quedar fuera.
- **`max_energy_rating`**: certificado energético mínimo, de `A` (mejor) a `G` (peor);
  `"B"` acepta A o B. Es un filtro duro cuando el portal publica la etiqueta
  (`energy_rating` en la respuesta). Para leerla, el scraper activa `fetchDetails` en
  Idealista, que añade una petición por inmueble y hace la ejecución bastante más lenta;
  sube `APIFY_TIMEOUT_SECONDS` o baja `APIFY_MAX_ITEMS` si usas este filtro. Los anuncios
  sin etiqueta se descartan mientras el filtro esté activo: no se puede garantizar el
  mínimo. La letra se lee de `energyCertification.energyConsumption.type` (Idealista) o
  de `energyCertificate` (Fotocasa).
- **Distancia**: cada anuncio incluye `distance_km` (km al centroide del CP o a las
  coordenadas indicadas) cuando el portal publica lat/lon. El selector de la UI permite
  ordenar por "Distancia (más cerca primero)"; los anuncios sin coordenadas van al final.
- **Límites**: `radius_km` entre 1 y 50, CP de 5 dígitos, año entre 1800 y el actual + 1
  y etiqueta energética entre A y G.
- **Precio y superficie**: en Idealista se ajustan solos a sus escalones (p. ej. 90 m² →
  80 m²); el filtro duro mantiene el valor exacto que pediste.
- **Radio aproximado**: se mide desde el centroide del CP; el área real del código postal
  puede ser irregular. El scraper re-filtra por Haversine cuando el anuncio publica
  coordenadas.

### 6.2 Búsqueda completa

```bash
curl -X POST http://127.0.0.1:8000/api/v1/searches \
  -H "Content-Type: application/json" \
  -d '{
    "location": "Madrid",
    "portals": ["idealista", "fotocasa"],
    "max_price_eur": 1500,
    "min_size_m2": 70,
    "min_rooms": 2,
    "max_results": 5,
    "preferences": "luminoso, con ascensor, cerca del metro"
  }'
```

Respuesta real (`201 Created`):

```json
{
  "search_id": "329f986c-2bbd-405e-8ce6-8385428cac8d",
  "criteria_fingerprint": "dcdf871823d12cc2cf1db9cb5f1fa1ffd66ae19224ae06b97cd8e1d87c2844b1",
  "run_date": "2026-11-04",
  "created_at": "2026-11-04T22:59:02.086200Z",
  "trigger": "api",
  "status": "completed",
  "scraped_count": 2,
  "matched_count": 2,
  "discarded_count": 0,
  "listings": [
    {
      "portal": "idealista",
      "url": "https://www.idealista.com/inmueble/1/",
      "title": "Piso luminoso en Chamberí",
      "price_eur": 1200.0,
      "price_per_m2": 15.0,
      "size_m2": 80.0,
      "rooms": 2,
      "bathrooms": 1,
      "location": "Madrid",
      "relevance": 0.9,
      "reason": "Encaja con los criterios"
    }
  ]
}
```

### 6.3 Recuperar resultados previos

```bash
curl http://127.0.0.1:8000/api/v1/searches/329f986c-2bbd-405e-8ce6-8385428cac8d/results
```

### 6.3.1 Ver la última búsqueda guardada

Lo que pinta la UI al abrirse (misma forma que `POST /api/v1/searches`):

```bash
curl http://127.0.0.1:8000/api/v1/searches/latest
```

`404` con `SEARCH_NOT_FOUND` mientras no haya ninguna búsqueda guardada.

### 6.4 Disparar el job diario manualmente

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scheduled-searches/run
```

Primera llamada del día (`200`):

```json
{"executed": true, "reason": null, "result": { "...": "..." }}
```

Segunda llamada del mismo día (`200`, idempotente):

```json
{"executed": false, "reason": "already_executed_today", "result": null}
```

### 6.5 Propagar un `request_id` propio

```bash
curl -X POST http://127.0.0.1:8000/api/v1/searches \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: mi-traza-001" \
  -d '{"location":"Valencia"}'
```

El identificador aparece en la cabecera `X-Request-ID` de la respuesta y en todos los
logs de esa petición.

## 7. Cliente en Python

```python
import httpx

BASE_URL = "http://127.0.0.1:8000/api/v1"

criterios = {
    "location": "Barcelona",
    "portals": ["idealista", "fotocasa"],
    "min_price_eur": 900,
    "max_price_eur": 1600,
    "min_size_m2": 65,
    "min_rooms": 2,
    "max_results": 20,
    "preferences": "terraza y ascensor, evitar bajos",
}

with httpx.Client(base_url=BASE_URL, timeout=180.0) as client:
    response = client.post("/searches", json=criterios)
    response.raise_for_status()
    result = response.json()

    print(f"Búsqueda {result['search_id']}: {result['matched_count']} anuncios")
    for item in result["listings"]:
        print(
            f"- {item['price_eur']:.0f} € | {item['size_m2']} m² | "
            f"relevancia {item['relevance']:.2f} | {item['url']}"
        )

    # Recuperar más tarde
    again = client.get(f"/searches/{result['search_id']}/results").json()
    assert again["search_id"] == result["search_id"]
```

`timeout` alto: la búsqueda es **síncrona** e incluye el scraping (minutos) y la llamada
al LLM. Para integraciones en producción, considera hacer la llamada en segundo plano
desde tu cliente o disparar `/scheduled-searches/run`.

## 8. Interpretar la respuesta

| Campo | Significado |
|---|---|
| `search_id` | UUID de la ejecución. Úsalo para consultar los resultados después. |
| `criteria_fingerprint` | SHA-256 estable de los criterios. Base de la idempotencia del job diario. |
| `run_date` | Fecha local de ejecución (zona `TIMEZONE`). |
| `created_at` | Timestamp UTC de creación. |
| `trigger` | `api`, `scheduler` o `manual-job`. |
| `scraped_count` | Anuncios devueltos por los portales tras los filtros duros (precio, m², habitaciones). |
| `matched_count` | Anuncios aceptados (cumplen `must_have` y superan `RELEVANCE_THRESHOLD`). |
| `discarded_count` | `scraped_count - matched_count`. |
| `portals` | Aportación de cada portal solicitado: `[{"portal": "idealista", "matched": 0}]`. Un `matched: 0` significa que ese portal no aportó nada. |
| `warnings` | Avisos legibles; p. ej. `"idealista: sin anuncios; revisa el actor del portal y la ubicación"`. |
| `listings[].relevance` | Puntuación 0.0-1.0 del clasificador. Usa `>= 0.8` como "encaje claro". |
| `listings[].url` | **Enlace directo al anuncio en el portal**. Es el dato que abres en el navegador. |
| `listings[].reason` | Justificación del LLM en español. |
| `listings[].price_per_m2` | Precio/m² redondeado (mitad hacia arriba). `null` si falta la superficie. |

El orden de `listings` es por relevancia descendente; a igual relevancia, precio ascendente.

### 8.1 ¿Dónde se guardan los resultados?

Cada búsqueda se persiste como **un fichero JSON por `search_id`** en `RESULTS_DIR`
(por defecto `data/results/`):

```
data/results/
├── 0c9ba77a-d604-463b-8bc0-d586fb6769bf.json
└── 5e21c1b4-...json
```

Esos ficheros contienen los **enlaces de los anuncios** junto con su puntuación:

```json
{
  "search_id": "0c9ba77a-d604-463b-8bc0-d586fb6769bf",
  "run_date": "2026-11-05",
  "trigger": "api",
  "scraped_count": 8,
  "matched_count": 2,
  "criteria": { "location": "Pozuelo de Alarcón", "max_price_eur": 900000, "min_rooms": 4 },
  "listings": [
    {
      "portal": "idealista",
      "url": "https://www.idealista.com/inmueble/112983119/",
      "title": "Chalet adosado en Calle Emilio Coll, 22, Zona norte, Pozuelo de Alarcón",
      "price_eur": 875000.0,
      "size_m2": 210.0,
      "rooms": 4,
      "amenities": ["swimming_pool", "terrace", "air_conditioning", "storage_room"],
      "price_per_m2": 4166.67,
      "relevance": 0.95,
      "reason": "Chalet adosado en urbanización con piscina..."
    }
  ]
}
```

Propiedades del almacenamiento:

| Aspecto | Comportamiento |
|---|---|
| Sobrevive a reinicios | Sí: al arrancar se reconstruye el índice desde los ficheros. |
| `GET /searches/{id}/results` | Funciona después de reiniciar (verificado). |
| Idempotencia del job diario | Se conserva entre reinicios. |
| Escritura | Atómica (fichero temporal + `os.replace`): nunca queda un JSON a medias. |
| `raw` del portal | **No** se guarda (voluminoso y puede contener datos de contacto). |
| Fichero corrupto | Se ignora con un `WARNING`; el resto sigue disponible. |
| Solo memoria | `RESULTS_DIR=` (vacío) desactiva la persistencia. |

Para abrir los enlaces guardados de la última búsqueda:

```bash
# PowerShell
Get-ChildItem data/results/*.json |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content |
  ConvertFrom-Json |
  ForEach-Object { $_.listings.url }
```

```bash
# bash / jq
jq -r '.listings[].url' "$(ls -t data/results/*.json | head -1)"
```

Los ficheros se escriben en `data/`, que está en `.gitignore`: no se versionan.

El umbral mínimo para considerar un anuncio relevante es `RELEVANCE_THRESHOLD`
(por defecto `0.6`, configurable en `.env`).

## 9. Errores y cómo resolverlos

Todos los errores comparten el mismo envoltorio:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "La petición no es válida",
    "request_id": "32afd95b-f894-433c-a579-c2dd045ece19",
    "details": {
      "fields": [{"loc": ["body", "location"], "msg": "String should have at least 2 characters"}]
    }
  }
}
```

| HTTP | `code` | Causa habitual | Solución |
|---|---|---|---|
| `422` | `VALIDATION_ERROR` | Body inválido, `min_price_eur > max_price_eur`, UUID mal formado, radio sin origen, CP+coordenadas o año fuera de rango | Corrige el campo señalado en `details.fields` |
| `422` | `POSTAL_CODE_NOT_FOUND` | El geocodificador no encuentra el código postal | Revisa `postal_code` |
| `422` | `INVALID_POSTAL_CODE` | CP con formato distinto de 5 dígitos | Usa un CP español de 5 dígitos |
| `404` | `SEARCH_NOT_FOUND` | `search_id` inexistente (o `RESULTS_DIR` vacío y proceso reiniciado) | Relanza la búsqueda |
| `502` | `SCRAPER_FAILURE` | Token de Apify inválido, actor inexistente, cuota agotada, timeout o **input rechazado por el actor** | Revisa los logs: `error_type` y el mensaje de Apify indican el campo inválido |
| `502` | `GEO_LOCATION_FAILURE` | CartoCiudad no responde o devuelve una respuesta no procesable | Reintenta o revisa `GEOCODING_BASE_URL` y `GEOCODING_TIMEOUT_SECONDS` |
| `503` | `CLASSIFIER_FAILURE` | Clave de DeepSeek inválida, cuota, respuesta no JSON | Revisa `DEEPSEEK_API_KEY` y `DEEPSEEK_MODEL` |
| `500` | `CONFIGURATION_ERROR` | Faltan variables obligatorias | Rellena `.env` |
| `500` | `INTERNAL_ERROR` | Fallo inesperado | Busca `unhandled_exception` en los logs con el `request_id` |

Cómo depurar: toma el `request_id` de la respuesta y búscalo en los logs.

```bash
# los logs incluyen request_id, search_id, duration_ms y error_code
uvicorn app.main:app 2>&1 | grep 32afd95b
```

### 9.1 La respuesta trae anuncios de un solo portal

No es un error HTTP: la búsqueda termina `201` porque uno de los portales **sí** respondió.
La respuesta lo delata:

```json
{
  "scraped_count": 20,
  "matched_count": 5,
  "portals": [
    {"portal": "idealista", "matched": 0},
    {"portal": "fotocasa", "matched": 5}
  ],
  "warnings": ["idealista: sin anuncios; revisa el actor del portal y la ubicación"]
}
```

Causas habituales, en orden de frecuencia:

1. **La ubicación no existe para ese portal.** Los actores resuelven nombres propios, y
   algunas formas compuestas fallan: Idealista **no** reconoce `"Madrid, Madrid"` ni
   `"madrid-capital"`; sí `"Madrid"`. El actor termina en `SUCCEEDED` con 0 items (no
   lanza excepción), de ahí que antes pasara desapercibido. El scraper ya recorta el
   sufijo de provincia para Idealista, pero si usas un municipio poco común prueba con el
   nombre suelto (`"Pozuelo de Alarcón"`, no `"Pozuelo de Alarcón, Madrid"`).
2. **El actor del portal no está configurado** (`APIFY_FOTOCASA_ACTOR=` vacío): el portal
   se omite con `scraper_portal_skipped`.
3. **Los filtros son demasiado estrictos** para ese portal (`min_size_m2`, `min_rooms`,
   `must_have`): el anuncio existe pero no se publica lo que exiges.

En los logs, los eventos que lo explican son
`scraper_portal_returned_no_listings` (el actor no devolvió nada, con la ubicación
resuelta), `scraper_portal_skipped` (sin actor) y `search_portal_without_results` (hubo
anuncios, pero ninguno pasó los filtros).

## 10. Búsqueda diaria programada

Se ejecuta a las **07:00** en la zona `TIMEZONE` (por defecto `Europe/Madrid`), arrancada
en el `lifespan` de FastAPI.

> **Prioridad:** si `SEARCH_PROFILE_JSON` está definido (lo está por defecto, ver
> [sección 11](#11-perfil-de-búsqueda-configurable-y-ui)), el cron usa **ese** perfil y las
> variables `DAILY_*` de abajo se ignoran. `DAILY_*` solo aplica con
> `SEARCH_PROFILE_JSON=` vacío.

```dotenv
ENABLE_SCHEDULER=true
TIMEZONE=Europe/Madrid

DAILY_SEARCH_LOCATION=Madrid
DAILY_SEARCH_PORTALS=idealista,fotocasa
DAILY_MAX_PRICE_EUR=1500
DAILY_MIN_SIZE_M2=60
DAILY_MIN_ROOMS=2
DAILY_MAX_RESULTS=40
DAILY_PREFERENCES=
# Requisitos OBLIGATORIOS (AND), separados por comas. Vacío = sin filtro duro.
# Ejemplo: piscina,padel
DAILY_MUST_HAVE=
# Términos deseables (no excluyentes), separados por comas: se destacan y van primero.
DAILY_PREFERRED_TERMS=
# Radio geográfico: CP y radio deben configurarse juntos (vacío = sin radio).
DAILY_POSTAL_CODE=
DAILY_RADIUS_KM=
# Año mínimo de construcción (vacío = sin filtro).
DAILY_MIN_YEAR_BUILT=
# Certificado energético mínimo: A (mejor) - G (peor); "B" = B o mejor.
DAILY_MAX_ENERGY_RATING=
```

**Radio y año en el cron:** si defines `DAILY_POSTAL_CODE`, debes indicar también
`DAILY_RADIUS_KM` (1-50). El CP se resuelve al arrancar la búsqueda con CartoCiudad y los
actores reciben el radio. `DAILY_MIN_YEAR_BUILT` aplica el mismo criterio que en la API:
duro si el portal publica el año y reforzado por el LLM cuando no consta. Un CP inválido,
un radio fuera de rango o configurar solo uno de los dos impide arrancar con
`CONFIGURATION_ERROR`.

**`DAILY_MUST_HAVE` vs `DAILY_PREFERENCES`:** `DAILY_MUST_HAVE` es un filtro **duro**
antes del LLM (si el anuncio no menciona el término en título, descripción o comodidades,
se descarta); `DAILY_PREFERENCES` solo suma relevancia. Para un chalet con piscina y
pádel obligatorios:

```dotenv
DAILY_PREFERENCES=Chalet con piscina y pista de pádel
DAILY_MUST_HAVE=piscina,padel
```

Los términos admiten espacios y no distinguen mayúsculas ni acentos; se aplica el mismo
contrato que en la API (2-40 caracteres, sin duplicados). Un valor inválido impide
arrancar con `CONFIGURATION_ERROR` y el término señalado en `details`.

**Idempotencia:** solo se ejecuta una vez por `(criteria_fingerprint, run_date)`. Si ya
existe, se registra `scheduled_search_skipped_duplicate` (WARNING) y la petición devuelve
`executed: false`. Cambiar cualquiera de los criterios diarios genera una nueva huella y
permite una ejecución distinta el mismo día.

**Desactivarlo** (por ejemplo si ejecutas varias réplicas y solo una debe llevar el cron):

```dotenv
ENABLE_SCHEDULER=false
```

El job nunca propaga excepciones: cualquier fallo se registra con `logger.exception` y
`error_code` para no perder el error.

## 11. Perfil de búsqueda configurable y UI

El perfil lo usan el job diario, la UI y `POST /api/v1/search-profile/run`. El valor de
fábrica vive en **una sola variable**, `SEARCH_PROFILE_JSON`:

```dotenv
SEARCH_PROFILE_JSON={"location":"Madrid","portals":["idealista","fotocasa"],"max_price_eur":900000,"min_size_m2":140,"min_rooms":4,"max_results":50,"preferences":"Casa o chalet, padel cominitario, piscina comunitaria","must_have":["piscina"],"preferred_terms":["padel"],"postal_code":"28031","radius_km":5,"min_year_built":2020,"max_energy_rating":"B"}
```

| Acción | Cómo |
|---|---|
| Ver el perfil activo | `curl http://127.0.0.1:8000/api/v1/search-profile` (`source` indica el origen) |
| Ejecutarlo | `curl -X POST http://127.0.0.1:8000/api/v1/search-profile/run` |
| **Cambiarlo desde la UI** | formulario «Editar criterios» → **Guardar criterios** |
| Cambiarlo por API | `curl -X PUT .../search-profile -H 'Content-Type: application/json' -d @perfil.json` |
| Restaurar el preconfigurado | `curl -X DELETE http://127.0.0.1:8000/api/v1/search-profile` |
| Volver a `DAILY_*` | deja `SEARCH_PROFILE_JSON=` vacío y borra el perfil guardado |

```bash
# Guardar un perfil (mismo cuerpo que POST /api/v1/searches)
curl -X PUT http://127.0.0.1:8000/api/v1/search-profile \
  -H "Content-Type: application/json" \
  -d '{"location":"Valencia","portals":["idealista"],"postal_code":"46001","radius_km":3,
       "max_price_eur":600000,"min_size_m2":120,"min_rooms":4,"must_have":["piscina"]}'

# Restaurar el preconfigurado
curl -X DELETE http://127.0.0.1:8000/api/v1/search-profile
```

Precedencia: **perfil guardado** (`source: override`) > `SEARCH_PROFILE_JSON`
(`source: profile`) > `DAILY_*` (`source: legacy`). El perfil guardado se escribe en
`search-profile.json` junto al directorio de resultados (`/data/search-profile.json` en
Kubernetes), así que lo comparten la UI y el cron y sobrevive a los reinicios.

Formato: el mismo cuerpo que `POST /api/v1/searches`, en **una línea**. Si el JSON de
configuración es inválido o incumple el contrato, el arranque falla con
`CONFIGURATION_ERROR` y `details.fields` indica el campo; un `PUT` inválido devuelve `422`
y no toca el perfil guardado.

### La UI

Abre <http://127.0.0.1:8000/>. La página muestra los criterios activos (CP, radio, precio,
m², habitaciones, año, obligatorios), un panel **Editar criterios** y un botón
**Ejecutar búsqueda** que llama a `/api/v1/search-profile/run`.

Al abrirse carga `GET /api/v1/searches/latest`, así que **siempre se ven los resultados de
la última búsqueda guardada**, aunque se abra desde otro dispositivo o después de reiniciar
el navegador (por eso el móvil ya no aparece vacío).

Cada tarjeta incluye:

- **Foto** del anuncio (seis como máximo; si el portal bloquea el hotlink se muestra
  «Sin foto»).
- Precio, €/m², superficie, habitaciones, baños y año de construcción.
- Ubicación, portal, comodidades detectadas, motivo del LLM y relevancia.
- Enlace directo a Idealista/Fotocasa.

Puedes ordenar por relevancia, precio, superficie o año sin volver a lanzar la búsqueda.
La UI no tiene build ni dependencias externas: son tres ficheros estáticos en `app/web/`.

> **Fotos**: se enlazan las URLs del portal (`listing.photos`, máximo 6). No se almacenan
> binarios ni se reenvían a terceros.

## 12. Despliegue en Kubernetes (ARM64)

```bash
# 1. Imagen (multi-arch; ARM64 explícito)
docker buildx build --platform linux/arm64 -t ghcr.io/<org>/house-finder:0.1.0 --push .

# 2. Secret con los tokens (nunca en git)
kubectl -n house-finder create secret generic house-finder-secrets \
  --from-literal=APIFY_TOKEN='...' --from-literal=DEEPSEEK_API_KEY='...'

# 3. Manifiestos
kubectl apply -k deploy/k8s
kubectl -n house-finder rollout status deploy/house-finder

# 4. UI
kubectl -n house-finder port-forward svc/house-finder 8000:80
# http://127.0.0.1:8000/
```

Qué incluye `deploy/k8s/`:

| Recurso | Detalle |
|---|---|
| `Deployment` | `nodeSelector: kubernetes.io/arch: arm64`, `replicas: 1`, `strategy: Recreate`, probes `/health`, usuario no root. |
| `ConfigMap` | Entorno + `SEARCH_PROFILE_JSON` (cambiar la búsqueda sin rebuild). |
| `Secret` | `APIFY_TOKEN` y `DEEPSEEK_API_KEY` (plantilla en `secret.example.yaml`). |
| `PVC` | 1 Gi en `/data` para `RESULTS_DIR=/data/results`. |
| `Service` | `ClusterIP` puerto 80 → 8000. |

`replicas: 1` es intencionado: APScheduler corre dentro del proceso y dos pods duplicarían
el job diario. Ver [deploy/k8s/README.md](../deploy/k8s/README.md).

## 13. Modo sin credenciales (tests)

`ENVIRONMENT=test` omite la validación de credenciales, de modo que puedes explorar la API
sin tokens (las llamadas reales a Apify/DeepSeek fallarán con `502`/`503`):

```bash
# PowerShell
$env:ENVIRONMENT="test"; $env:ENABLE_SCHEDULER="false"; uvicorn app.main:app --reload
```

```bash
# bash
ENVIRONMENT=test ENABLE_SCHEDULER=false uvicorn app.main:app --reload
```

La suite completa no necesita red: los ports se sustituyen por fakes.

```bash
pytest
pytest app/tests/test_api.py -v          # solo integración de la API
pytest -k "scheduled" -v                 # solo el job diario
```

---

## Resumen de variables

| Variable | Por defecto | Obligatoria | Notas |
|---|---|---|---|
| `ENVIRONMENT` | `development` | no | `production` activa logs JSON |
| `LOG_LEVEL` | `INFO` | no | |
| `TIMEZONE` | `Europe/Madrid` | no | zona del cron |
| `ENABLE_SCHEDULER` | `true` | no | |
| `APIFY_TOKEN` | — | **sí** | |
| `APIFY_IDEALISTA_ACTOR` | `igolaizola/idealista-scraper` | no | `usuario/actor` verificado |
| `APIFY_FOTOCASA_ACTOR` | vacío (omitido) | no | `igolaizola/fotocasa-scraper` (pay-per-event) |
| `SEARCH_OPERATION` | `rent` | no | `rent` \| `sale` |
| `APIFY_MAX_ITEMS` | `40` | no | 1-200 |
| `APIFY_TIMEOUT_SECONDS` | `300` | no | 10-900 |
| `APIFY_MAX_CHARGE_USD` | sin límite | no | tope de gasto por ejecución (USD) |
| `GEOCODING_BASE_URL` | `https://www.cartociudad.es/geocoder/api/geocoder` | no | geocodificador de CP |
| `GEOCODING_TIMEOUT_SECONDS` | `10` | no | segundos |
| `DEEPSEEK_API_KEY` | — | **sí** | |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | no | |
| `DEEPSEEK_MODEL` | `deepseek-flash` | no | |
| `DEEPSEEK_TIMEOUT_SECONDS` | `60` | no | |
| `RELEVANCE_THRESHOLD` | `0.6` | no | 0.0-1.0 |
| `RESULTS_DIR` | `data/results` | no | directorio de JSON; vacío = solo memoria |
| `SEARCH_PROFILE_JSON` | cuerpo CP 28031 + 5 km + 2020 | no | perfil único (cron + UI); vacío = `DAILY_*` |
| `DAILY_*` | ver sección 10 | no | criterios del cron (solo sin perfil) |
| `DAILY_MUST_HAVE` | vacío | no | requisitos obligatorios del cron (`piscina,padel`) |
| `DAILY_PREFERRED_TERMS` | vacío | no | deseables no excluyentes; se destacan primero |
| `DAILY_POSTAL_CODE` | vacío | no | requiere `DAILY_RADIUS_KM` |
| `DAILY_RADIUS_KM` | vacío | no | 1-50 km |
| `DAILY_MIN_YEAR_BUILT` | vacío | no | 1800-año actual+1 |
| `DAILY_MAX_ENERGY_RATING` | vacío | no | A-G (`B` = B o mejor) |
