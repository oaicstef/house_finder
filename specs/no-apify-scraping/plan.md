# Plan — Scraper propio por portal (sin depender de Apify)

## Objetivo

Obtener las mismas funcionalidades (Idealista + Fotocasa, filtros duros, radio por CP, año,
tipo de vivienda, certificado energético, `must_have`, fotos, coordenadas, clasificación LLM,
job diario y UI) **sin que Apify sea obligatorio**, sin tocar dominio, servicios, API ni UI.
Apify queda como proveedor opcional y red de seguridad por portal.

## Evidencia que fija el diseño

Medido en vivo (IP residencial, UA de navegador):

| Comprobación | Resultado |
| --- | --- |
| `GET https://www.idealista.com/venta-viviendas/madrid-madrid/` | **403**, `server: DataDome`, `x-datadome: protected` |
| `GET https://www.fotocasa.es/es/comprar/viviendas/madrid-capital/todas-las-zonas/l` | **200**, 1,6 MB con `window.__INITIAL_DATA__` y `<script id="__initial_props__">` → `initialSearch.result.realEstates` (31 anuncios: `rawPrice`, `features.surface/rooms/bathrooms/antiquity`, `coordinates`, `multimedia`, `description`) |
| `GET ...?maxPrice=&minPrice=&minSurface=&minRooms=&page=2` | filtros y paginación **aceptados** por el portal (`params` los refleja y cambia el `count`) |
| `POST https://api.idealista.com/oauth/token` | **401** → existe API oficial para partners |

Conclusión: Fotocasa se puede sustituir por HTTP directo; Idealista necesita navegador con
IP/UA coherentes o la API oficial. Cualquier diseño que prometa "Idealista por HTTP" falla.

## Diseño implementado

```
ScraperPort
└── ListingScraper                     app/infrastructure/scrapers/listing_scraper.py
    ├── sources: RawListingSource       (una por portal, devuelve items crudos)
    │   ├── sources/fotocasa.py         HTTP directo + parseo de __initial_props__
    │   ├── sources/idealista_api.py    Search API oficial (opt-in, acceso aprobado)
    │   └── sources/apify.py            actor de Apify (proveedor opcional)
    ├── delegates: ScraperPort          (portal servido por otro proveedor / respaldo)
    ├── normalization.py                item crudo -> Listing (sin saber de proveedores)
    └── filtering.py                    filtros duros (radio, año, energía, precio, m², hab.)
```

- `ListingScraper` reproduce el comportamiento histórico: dedupe por URL, orden de filtros,
  contadores y eventos (`scraper_portal_skipped`, `listings_filtered_by_*`, `listings_scraped`,
  `scraper_portal_returned_no_listings`), y `SCRAPER_FAILURE` si ningún portal responde.
- `factory.build_scraper()` es el único sitio que interpreta `SCRAPER_PROVIDERS`
  (`portal=local|idealista-api|apify|none`); los portales no listados quedan desactivados.
- `APIFY_TOKEN` pasa a ser obligatorio **solo** si algún portal usa `apify`.
- `sources/http.py` centraliza UA, timeout, reintentos con backoff (429/5xx), `Retry-After`,
  límite de peticiones y proxy opcional.

## Capacidades por portal (contrato)

| Capacidad | Idealista (`apify` / API oficial opt-in) | Fotocasa (`local`) |
| --- | --- | --- |
| `rent` / `sale` | sí | sí (`/es/alquiler/`, `/es/comprar/`) |
| Ubicación por texto | sí | sí (segmento de ciudad + filtro de radio local) |
| Precio / m² / habitaciones | sí (servidor) | sí (servidor y local) |
| Radio por coordenadas | sí | local (el portal ignora `radius`) |
| Tipo de vivienda | sí | portal omitido si se piden tipos (igual que antes) |
| Año de construcción | sí si lo publica | desde `antiquity` |
| Certificado energético | sí (detalle) | no publicado (mismo descarte) |
| Descripciones para `must_have` | sí (detalle) | sí (en la búsqueda) |
| Fotos, título, coordenadas, `raw` | sí | sí |

## Estado

- **Hecho (F1–F3)**: núcleo extraído (`normalization`, `filtering`, `listing_scraper`,
  `sources/base`), fuente Fotocasa con paginación/filtros/reintentos, fábrica y configuración
  por portal, respaldo a Apify, `APIFY_TOKEN` condicional, probe de operación y tests con
  fixtures.
- **F4 parcialmente implementado**: existe `IdealistaApiSource`, OAuth cacheado, paginación,
  mapeo y selección por configuración, con tests sin red. No se dispone de credenciales de
  partner ni documentación accesible del contrato Search; los supuestos de endpoint, filtros y
  respuesta no están validados contra Idealista. Mantener Idealista en Apify o desactivado hasta
  probarlo con acceso oficial aprobado. No se implementa evasión de DataDome.
- **Pendiente (F5)**: retirar Apify solo después de validar la API oficial de Idealista en el
  entorno autorizado y comprobar paridad de campos y búsquedas reales.

## Validación

`pytest` completo sin red (fixtures recortadas en `app/tests/fixtures/fotocasa/`), más
`python -m app.tools.scraper_probe --portal fotocasa --location Madrid` para la prueba real.
La vía Idealista requiere prueba separada con credenciales aprobadas: token OAuth, respuesta
Search, filtros, paginación y campos de detalles. Hasta entonces no se considera sustituido
Apify para Idealista.

## Riesgos

| Riesgo | Mitigación |
| --- | --- |
| Bloqueo o cambio de HTML de Fotocasa | `SCRAPER_FAILURE` + respaldo a Apify + probe de diagnóstico + fixtures que detectan cambios en tests |
| IP de datacenter bloqueada | `SCRAPER_PROXY_URL` (proxy/relay residencial) y límite de peticiones |
| Pérdida de capacidades (energía, tipos) | tabla de capacidades y `warnings` por portal |
| Doble disparo UI + cron | caché y rate limit pendientes de evaluar con el perfil real |
