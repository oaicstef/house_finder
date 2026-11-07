# Plan: pipeline de despliegue en GitHub Actions

## Objetivo

Dotar al repositorio de un pipeline de CI/CD equivalente al del proyecto `utopia`:
cobertura de tests en cada PR/push, escaneo de seguridad programado, publicación de la
imagen Docker en GHCR y despliegue automático en Kubernetes ARM64 desde `main`, con
releases de GitHub al empujar un tag `v*`. El objetivo es que ningún cambio llegue al
clúster sin pasar por tests y análisis, y que el despliegue sea reproducible por commit.

## Alcance

### Dentro del alcance

- Workflow de cobertura y tests (`coverage.yml`) con artefacto HTML y resumen en el job.
- Workflow de seguridad (`security.yml`): `pip-audit`, Trivy filesystem/IaC y Trivy imagen.
- Workflow de build, push a GHCR y despliegue (`release.yml`).
- `dependabot.yml` agrupado para pip, GitHub Actions y la imagen base Docker.
- Configuración de cobertura reutilizable (`.coveragerc`) y `pytest-cov` en dev.
- Ajuste de los manifiestos k8s para consumir `ghcr.io/oaicstef/house-finder` con
  `imagePullSecrets`.
- Documentación del pipeline y de los secretos requeridos.

### Fuera del alcance (out of scope)

- Provisionar el clúster, el ingress o el certificado TLS.
- Rotación automática de secretos (`APIFY_TOKEN`, `DEEPSEEK_API_KEY`).
- Despliegues multi-entorno (staging/producción) y aprobaciones manuales.
- Escaneo CodeQL o firma de imágenes (cosign/SBOM).
- Cambiar el Dockerfile o la arquitectura de la aplicación.

## Decisiones de arquitectura

| Decisión | Elección | Justificación |
|---|---|---|
| Espejo del pipeline | Misma estructura que `utopia`: `coverage.yml`, `security.yml`, `release.yml`, `dependabot.yml` | Coherencia entre repos del mismo autor; mismo modelo mental y mismos nombres de secretos. |
| Runtime Python | `actions/setup-python@v5` con Python 3.13 y caché de pip | Coincide con `python:3.13-slim` del Dockerfile. |
| Cobertura | `pytest-cov` + `.coveragerc` (sin umbral que rompa el build) | Igual que `utopia`, que publica cobertura sin `fail_under`; evita bloquear PRs por ruido. |
| SCA | `pip-audit -r requirements*.txt` con gate | Equivalente a `npm audit --audit-level=high`. |
| IaC e imagen | `aquasecurity/trivy-action@v0.36.0`, severidad HIGH/CRITICAL, `ignore-unfixed` | Mismo pin y misma configuración que `utopia`. |
| Registro | GHCR (`ghcr.io/<owner>/house-finder`) con `GITHUB_TOKEN` para push | Sin credenciales extra para publicar; el clúster usa un pull secret. |
| Arquitectura de imagen | Solo `linux/arm64` | El Deployment fija `nodeSelector: kubernetes.io/arch: arm64`; `utopia` también publica sua imagen de frontend solo arm64. |
| Tags de imagen | `latest`, `latest-arm64`, `<ref>` y `sha-<commit>` | `sha-*` da despliegues reproducibles aun con `imagePullPolicy: IfNotPresent`. |
| Disparador de deploy | `push` a `main`, tags `v*` y `workflow_dispatch` | `main` despliega; los tags publican además un GitHub Release. |
| Concurrencia | `coverage`/`security` cancelan ejecuciones previas; `deploy` no | Cancelar un rollout a medias deja el pod inconsistente. |
| Release | `softprops/action-gh-release@v2` solo en `refs/tags/v*` | Notas generadas automáticamente; requiere `contents: write`. |

## Contratos de interfaces (ports)

No aplica: el cambio no toca dominio, puertos ni servicios. Los "contratos" son los
nombres de secretos y los tags de imagen:

```text
Secrets requeridos:
  KUBE_CONFIG          # kubeconfig del clúster (base64 o YAML, según azure/k8s-set-context)
  REGISTRY_USERNAME    # usuario GHCR para el pull secret del clúster
  REGISTRY_PASSWORD    # PAT con read:packages
  GITHUB_TOKEN         # implícito: push a GHCR y GitHub Release

Imagen:
  ghcr.io/oaicstef/house-finder:{latest,latest-arm64,<ref>,sha-<7 chars>}

Pull secret en el clúster:
  namespace house-finder -> secret ghcr-login-secret (docker-registry ghcr.io)
```

## Modelo de datos

No aplica. No hay entidades ni esquemas Pydantic nuevos. La única persistencia es el PVC
existente (`house-finder-results`).

## Endpoints de la API

No aplica. El pipeline consume el endpoint existente `GET /health` a través de las probes
`readinessProbe` y `livenessProbe`; un fallo de arranque hace fallar `kubectl rollout status`.

## Flujo del job programado

No aplica al pipeline. El scheduler interno de la app (07:00 `Europe/Madrid`) no cambia.
El `security.yml` se programa semanalmente los lunes a las 06:00 UTC.

## Estrategia de logging

- Los workflows no añaden logging de aplicación.
- La cobertura se publica en el **job summary** (`$GITHUB_STEP_SUMMARY`) y como artefacto
  `coverage-report` (HTML + XML, 14 días).
- Los informes de `pip-audit` se archivan como JSON (30 días) aunque el gate falle.
- Trivy imprime tabla en el log del job con severidad HIGH/CRITICAL.
- Nunca se imprimen secretos: `REGISTRY_PASSWORD` y `KUBE_CONFIG` solo se pasan por `env`/inputs.

## Jerarquía de excepciones

No aplica código de aplicación. Fallos esperados del pipeline y su traducción:

| Fallo | Efecto |
|---|---|
| Tests o cobertura fallan | Job `coverage` en rojo; el PR no debe mergearse si la rama lo exige. |
| Advisory conocido (`pip-audit`) | Job `dependencies` en rojo. |
| Vulnerabilidad o misconfiguración HIGH/CRITICAL | Job Trivy en rojo. |
| Build/push de imagen falla | Job `image` en rojo; `deploy` no se ejecuta (`needs`). |
| Rollout no converge en 180 s | Job `deploy` en rojo; el clúster conserva la revisión anterior si el pod no arranca. |

## Plan de tests

- **Local**: `pytest --cov` con el `.venv`; sin llamadas reales a Apify ni DeepSeek.
- **CI**: el job `coverage` ejecuta la misma suite con `pytest-cov`; los tests usan fakes de
  los ports, por lo que no requieren secretos.
- **Validación del pipeline**: parseo YAML de los cuatro ficheros, `docker build` local de la
  imagen y arranque con `ENVIRONMENT=test` verificando `/health` y `/`.
- **Validación en GitHub**: tras el push, comprobar que los workflows aparecen y que
  `coverage`/`security` pasan en `main`.

## Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| `pip-audit` detecta un advisory en dependencias con rango `>=` | Pipeline en rojo sin cambios de código | Gate real (deseado); el informe JSON indica el paquete y la versión a subir. |
| Build arm64 bajo QEMU es lento | Jobs de 10-20 min | Caché `type=gha` y `cache-to: mode=max`; solo se compila arm64. |
| El paquete GHCR es privado | `ImagePullBackOff` | `imagePullSecrets: ghcr-login-secret`, creado por el workflow si falta. |
| Tag `sha-*` no existe si el job `image` falla | Deploy no se ejecuta | `deploy` depende de `needs: [image]`. |
| `kubectl apply -k` no reinicia el pod si solo cambia el ConfigMap | Configuración no aplicada | Cada commit genera un tag `sha-*` distinto, por lo que `set image` siempre provoca rollout. |
| Clúster sin `KUBE_CONFIG` | Deploy en rojo | Documentado en `deploy/k8s/README.md`; el pipeline de cobertura/seguridad sigue funcionando. |
| Dos pods con scheduler | Job diario duplicado | Se mantienen `replicas: 1` y `strategy: Recreate`. |

## Orden de implementación

1. [x] `requirements-dev.txt` + `.coveragerc` + `.gitignore` — cobertura local reproducible — `pytest --cov` genera `htmlcov/` y `coverage.xml` sin ensuciar git.
2. [x] `.github/workflows/coverage.yml` — tests y cobertura en PR/push a `main` — YAML válido y job con resumen + artefacto.
3. [x] `.github/workflows/security.yml` — `pip-audit`, Trivy fs/IaC e imagen — YAML válido; gates HIGH/CRITICAL.
4. [x] `.github/workflows/release.yml` — build/push arm64, deploy k8s y release por tag — YAML válido; imágenes con tag `sha-*`.
5. [x] `.github/dependabot.yml` — actualizaciones agrupadas (pip, actions, docker) — YAML válido.
6. [x] `deploy/k8s/deployment.yaml` y `kustomization.yaml` — imagen GHCR + `imagePullSecrets` — `kubectl kustomize deploy/k8s` renderiza la imagen correcta.
7. [x] `README.md` y `deploy/k8s/README.md` — documentar pipeline, secretos y operación.

## Criterios de aceptación

- [x] Los cuatro ficheros de `.github` parsean como YAML válido.
- [x] La suite local sigue en verde con cobertura: `182 passed`, 91 % de líneas.
- [x] `pip-audit` sobre `requirements.txt` y `requirements-dev.txt`: sin vulnerabilidades conocidas.
- [x] `docker build` de la imagen y smoke test del contenedor: `/health` y `/` responden 200.
- [x] `kubectl kustomize deploy/k8s` renderiza `ghcr.io/oaicstef/house-finder` y el pull secret.
- [ ] GitHub registra los workflows y `coverage`/`security` pasan en `main` tras el push.
- [ ] Un push a `main` publica `ghcr.io/oaicstef/house-finder:sha-<commit>` y completa el rollout.
- [ ] Un tag `v*` publica además un GitHub Release.

## Checklist de progreso

- [x] 1. Cobertura local (`requirements-dev.txt`, `.coveragerc`, `.gitignore`).
- [x] 2. Workflow de cobertura.
- [x] 3. Workflow de seguridad.
- [x] 4. Workflow de release y despliegue.
- [x] 5. Dependabot agrupado.
- [x] 6. Manifiestos k8s apuntando a GHCR.
- [x] 7. Documentación del pipeline.
- [ ] 8. Verificación en GitHub Actions tras el push.
