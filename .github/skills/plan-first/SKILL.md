---
name: plan-first
description: 'Plan-first workflow before writing feature code. Use when starting any new feature, refactor or backend module: creates specs/<feature-slug>/plan.md (architecture decisions, ports, data model, API contracts, logging, exceptions, tests, risks, implementation order, progress checklist) and requires the plan to be complete before any source file is generated. Also use when the user says "aplica plan-first", "planifica antes de codificar" or asks for a spec-driven implementation.'
argument-hint: '<feature-slug> — p. ej. real-estate-search-agent'
user-invocable: true
---

# Plan-First

Garantiza que ninguna feature se implemente sin un plan aprobado y versionado en el repositorio.
El plan es el contrato: si el código y el plan divergen, se actualiza el plan (nueva revisión) o se corrige el código.

## Cuándo aplicar

- Nueva feature, módulo, endpoint o integración.
- Refactor con impacto en más de un archivo.
- Cualquier petición que diga "aplica la skill plan-first para `<feature>`".

## Procedimiento

1. **Slug**: deriva el slug en kebab-case (`real-estate-search-agent`, `billing-webhooks`, ...).
2. **Crear o revisar** `specs/<feature-slug>/plan.md`:
   - Si **no existe**: créalo con la plantilla completa de abajo.
   - Si **existe**: **no lo sobrescribas**. Añade al final una sección `## Revisión YYYY-MM-DD` (fecha actual) describiendo qué cambia y por qué, y actualiza las secciones afectadas dentro de esa revisión.
3. **Plantilla obligatoria** — todas las secciones, en este orden:

```markdown
# Plan: <nombre de la feature>

## Objetivo
<2-4 frases>

## Alcance
### Dentro del alcance
- ...
### Fuera del alcance (out of scope)
- ...

## Decisiones de arquitectura
| Decisión | Elección | Justificación |
|---|---|---|

## Contratos de interfaces (ports)
<firmas tipadas>

## Modelo de datos
<entidades de dominio + esquemas Pydantic request/response>

## Endpoints de la API
<método, ruta, request, response, códigos de error>

## Flujo del job programado
<pasos, idempotencia, manejo de errores>

## Estrategia de logging
<eventos clave, campos de contexto>

## Jerarquía de excepciones
<clase, code, traducción HTTP>

## Plan de tests
<qué con mocks, qué con integración>

## Riesgos y mitigaciones
| Riesgo | Impacto | Mitigación |
|---|---|---|

## Orden de implementación
1. [ ] Tarea — archivos — criterio de "done"

## Criterios de aceptación
- [ ] ...

## Checklist de progreso
- [ ] 1. ...
```

4. **Bloqueo de código**: no crear ni modificar archivos de código fuente de la feature hasta que `plan.md` contenga todas las secciones. El plan es la única salida permitida en la primera fase.
5. **Ejecución**: implementa las tareas del checklist **en orden**. Al terminar cada tarea, marca la casilla correspondiente en `## Checklist de progreso` (`- [ ]` → `- [x]`) y en `## Orden de implementación`.
6. **Validación**: ejecuta la suite de tests acordada en el plan y actualiza la sección de criterios de aceptación solo con evidencia real.
7. **Desviaciones**: si durante la implementación aparece una decisión nueva, añade una `## Revisión YYYY-MM-DD` al final del plan explicando el cambio. Nunca borres historial.

## Reglas duras

- Nunca sobrescribir un `plan.md` existente: es append-only salvo ediciones dentro de la revisión del día.
- Nunca generar código de la feature si el plan está incompleto.
- Cada tarea del orden de implementación debe nombrar archivo(s) y criterio de "done" verificable.
- El plan debe declarar explícitamente el *out of scope*.
