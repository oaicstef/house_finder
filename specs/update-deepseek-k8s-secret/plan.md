# Plan: Actualizar solo la clave API de DeepSeek en Kubernetes

## Objetivo
Preparar un script PowerShell que solicite la clave de DeepSeek de forma oculta y actualice únicamente `DEEPSEEK_API_KEY` en el Secret `house-finder-secrets`. Tras actualizarla, el script reiniciará y comprobará el Deployment `house-finder` para que la aplicación cargue el nuevo valor.

El script no debe mostrar ni guardar la clave en el historial del shell, en argumentos de procesos ni en los logs de ejecución.

## Alcance
### Dentro del alcance
- Crear un script PowerShell para operar sobre el namespace y recursos existentes de Kubernetes.
- Usar `kubectl patch` de tipo merge para modificar únicamente `data.DEEPSEEK_API_KEY`, dejando intactas las demás claves del Secret.
- Solicitar la clave sin eco, mantener el parche temporal fuera del repositorio y eliminarlo al terminar.
- Reiniciar y esperar el rollout del Deployment `house-finder`.
- Documentar prerrequisitos, ejecución y comportamiento en la guía de Kubernetes.

### Fuera del alcance (out of scope)
- Cambiar la clave en GitHub Actions, `.env`, el despliegue N100 u otros entornos.
- Crear o reemplazar el Secret, modificar otras claves, renovar la clave en DeepSeek o validar la clave con una llamada API.
- Cambiar la aplicación, imágenes, configuración de Kubernetes o credenciales de acceso al clúster.

## Decisiones de arquitectura
| Decisión | Elección | Justificación |
|---|---|---|
| Plataforma del script | PowerShell (`.ps1`) que invoca `kubectl` | El operador puede ejecutarlo directamente en Windows y mantener el comportamiento independiente de la aplicación. |
| Interacción con la clave | Solicitarla con `Read-Host -AsSecureString` | Evita eco en pantalla y que el valor se escriba como literal en el comando invocado. |
| Actualización | Parche JSON merge temporal del campo `data.DEEPSEEK_API_KEY` | Actualiza solo el campo solicitado y conserva las demás claves del Secret. |
| Aplicación en runtime | Reiniciar y esperar el Deployment `house-finder` | Las variables de entorno de un Pod no cambian al actualizar un Secret; el rollout crea un Pod con el valor nuevo. |
| Fallos | Detenerse ante error de `kubectl` y devolver estado no exitoso | No indicar éxito si el Secret no existe, el parche falla o el rollout no termina correctamente. |

## Contratos de interfaces (ports)
No aplica: script operativo local que depende del ejecutable `kubectl` y del contexto Kubernetes configurado por el usuario.

## Modelo de datos
No aplica a modelos de dominio ni esquemas Pydantic. El recurso existente es el Secret Kubernetes `house-finder-secrets`; el parche contiene exclusivamente `data.DEEPSEEK_API_KEY` con el valor codificado en Base64.

## Endpoints de la API
No aplica: no se modifican endpoints de la aplicación.

## Flujo del job programado
No aplica: no se modifica ni se ejecuta el job diario de búsqueda. El flujo manual del script es: validar `kubectl`, solicitar la clave oculta, aplicar el parche, borrar el archivo temporal, reiniciar el Deployment y esperar el rollout.

## Estrategia de logging
No se registrará la clave, su forma codificada ni el contenido del parche. Se mostrarán mensajes breves de progreso y errores explícitos de `kubectl`; el archivo de parche temporal se eliminará incluso si una operación falla.

## Jerarquía de excepciones
No aplica a excepciones de dominio o HTTP. Los fallos del script se expresarán mediante errores de PowerShell y código de salida distinto de cero, conservando el error de `kubectl` sin imprimir el contenido del secreto.

## Plan de tests
- Revisar estáticamente el script para confirmar que el parche contiene únicamente `DEEPSEEK_API_KEY`, que la clave no aparece en salida/argumentos y que el temporal se limpia en una ruta `finally`.
- Validar que el documento de ejecución coincide con los nombres de Secret, namespace y Deployment de los manifiestos.
- No realizar una prueba de integración contra un clúster ni usar una clave real; ejecutar las pruebas de la aplicación no aporta cobertura a este script operativo.

## Riesgos y mitigaciones
| Riesgo | Impacto | Mitigación |
|---|---|---|
| El archivo temporal contiene material sensible mientras se aplica el parche | Exposición local de la clave | Guardarlo en el directorio temporal del usuario, no mostrar su contenido y borrarlo en `finally`. |
| Actualizar el Secret no cambia el entorno del Pod activo | La aplicación continúa usando la clave anterior | Reiniciar el Deployment y esperar su rollout antes de reportar éxito. |
| Fallo durante el rollout después de aplicar el parche | El Secret queda actualizado, pero la aplicación puede seguir sin disponibilidad | Propagar el fallo y dejar claro que debe revisarse el estado del Deployment. |
| Contexto/namespace incorrecto | Actualización del recurso equivocado o error | Fijar `house-finder` como namespace y documentar revisar el contexto de `kubectl` antes de ejecutar. |

## Orden de implementación
1. [x] Crear `deploy/k8s/update-deepseek-key.ps1` — done cuando actualice solo `DEEPSEEK_API_KEY`, oculte la entrada y falle explícitamente en errores de parche o rollout.
2. [x] Documentar el uso en `deploy/k8s/README.md` — done cuando indique prerrequisitos, ejecución y que únicamente cambia la clave de DeepSeek.
3. [x] Revisar el parche y los documentos — done cuando los nombres de recursos coincidan con los manifiestos y la clave no se imprima ni quede versionada.

## Criterios de aceptación
- [x] El parche modifica solo `DEEPSEEK_API_KEY` y preserva las demás claves del Secret.
- [x] La clave se solicita sin eco y no se incluye en el historial del comando ni se imprime.
- [x] El parche temporal se elimina al finalizar incluso ante errores.
- [x] El script reinicia y espera correctamente el rollout; los errores devuelven un estado no exitoso.
- [x] La documentación explica requisitos y uso sin incluir credenciales.

## Checklist de progreso
- [x] 1. Crear el script PowerShell.
- [x] 2. Documentar la ejecución en la guía Kubernetes.
- [x] 3. Revisar coherencia y validar el resultado.
