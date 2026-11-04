# Instrucciones del workspace

## Plan primero, código después

Antes de implementar cualquier feature nueva, refactor con impacto en varios archivos o
módulo backend, aplica la skill `plan-first`:

1. Deriva el slug en kebab-case (`real-estate-search-agent`).
2. Crea o amplía `specs/<slug>/plan.md` siguiendo la plantilla de
   [`.github/skills/plan-first/SKILL.md`](skills/plan-first/SKILL.md).
3. Si el archivo ya existe, **no lo sobrescribas**: añade una sección
   `## Revisión YYYY-MM-DD` al final.
4. No generes archivos de código de la feature hasta que el plan esté completo.
5. Implementa el checklist en orden y marca las casillas a medida que avanzas.

## Convenciones del proyecto

- Arquitectura hexagonal: `domain` no importa FastAPI, Pydantic ni SDKs; los servicios
  dependen de `app/domain/ports.py`; las implementaciones viven en `app/infrastructure`.
- Comentarios y docstrings en español; nombres de clases, funciones y variables en inglés.
- Errores de negocio: subclases de `DomainException` con `code` y `http_status`.
- Logging con `structlog`; todo evento incluye `event`, `request_id`, `search_id`,
  `listing_url`, `duration_ms`, `error_code`. `WARNING` para errores de dominio
  esperados, `ERROR` + `logger.exception()` solo para lo inesperado.
- Nunca loguear tokens ni datos personales.
- Tests con `pytest` + `pytest-asyncio` y fakes de los ports: sin llamadas reales a
  Apify ni DeepSeek.
