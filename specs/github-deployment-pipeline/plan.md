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

## Revisión 2026-11-07

La primera ejecución real del pipeline en GitHub (`run 42412157361`) validó la cobertura y
`pip-audit`, pero el gate de Trivy falló con hallazgos reales que había que resolver:

| Hallazgo | Origen | Corrección |
|---|---|---|
| `KSV-0014` (HIGH) | `readOnlyRootFilesystem: false` en el Deployment | Se activa `true`; la app solo escribe en el PVC `/data`. Verificado con contenedor `--read-only`. |
| `CVE-2026-76844` (zlib1g) | Paquete Debian de la base con parche disponible | `apt-get -y upgrade` en el Dockerfile. |
| `CVE-2026-114613`, `GHSA-6v7p-g79w-8964` (msgpack), `CVE-2025-47273` (setuptools), `CVE-2026-97687/97689` (urllib3) | Librerías vendorizadas dentro de `pip` en la imagen | Se elimina `pip` y su `dist-info` tras instalar `requirements.txt`; el runtime no instala nada. |

Validación posterior: `trivy image` y `trivy fs` con `--exit-code 1` devuelven 0, y el
contenedor arranca con raíz de solo lectura respondiendo 200 en `/health` y `/`.
También se comprobó que el job de imagen del Release publica en GHCR; el job de despliegue
queda pendiente de los secretos `KUBE_CONFIG`, `REGISTRY_USERNAME` y `REGISTRY_PASSWORD`.

## Revisión 2026-11-07 (destino N100)

El destino de despliegue cambia de Kubernetes a **Docker Compose en el N100**, siguiendo
el enfoque del API de PadelReservation (checkout del repo en el servidor + `docker compose`
+ `restart`), pero automatizado desde `release.yml`.

| Decisión | Cambio |
|---|---|
| Plataforma de imagen | `linux/arm64` → `linux/amd64`: el N100 es x86_64. Se elimina el tag `latest-arm64`. |
| Job de despliegue | Sustituye `azure/k8s-set-context` + `kubectl` por `appleboy/ssh-action@v1`. |
| Flujo | `git fetch/pull --ff-only` en `N100_DEPLOY_PATH` → `deploy/n100/deploy.sh sha-<commit>` → `docker compose pull` + `up -d` → espera a `healthy`; si falla, muestra logs y el job falla. |
| Persistencia | Bind mount `deploy/n100/data:/data`; `deploy.sh` crea el directorio y lo hace escribible para el UID 10001. |
| Configuración | `deploy/n100/.env` (tokens y perfil) vive solo en el N100; `.env.example` versionado. |
| Secretos | `N100_HOST`, `N100_USER`, `N100_SSH_KEY` (obligatorios); `N100_PORT`, `N100_DEPLOY_PATH` (opcionales); `REGISTRY_*` solo si el paquete GHCR es privado. |
| Kubernetes | Los manifiestos de `deploy/k8s` quedan como alternativa documentada; ya no los usa el pipeline. |

Criterios de aceptación añadidos:

- [x] El job de imagen publica `ghcr.io/oaicstef/house-finder:sha-<commit>` para `linux/amd64` (run 42416874042).
- [x] La imagen amd64 arranca con raíz de solo lectura y `/data` escribible: `/health` y `/` responden 200 (verificado con emulación QEMU).
- [x] El comando del `healthcheck` del compose se ejecuta correctamente dentro del contenedor.
- [ ] El job de deploy actualiza el checkout del N100 y deja el contenedor `healthy` (pendiente de `N100_HOST`/`N100_USER`/`N100_SSH_KEY`).
- [ ] `http://<n100>:<HOST_PORT>/health` responde 200 tras el despliegue.
- [ ] Un tag `v*` publica además el GitHub Release.

## Revisión 2026-11-07 (vuelta a Kubernetes)

El destino vuelve a ser el **clúster Kubernetes ARM64**:

| Decisión | Cambio |
|---|---|
| Imagen | `linux/amd64` → `linux/arm64` (nodo ARM64); se recupera el tag `latest-arm64`. |
| Job de despliegue | `appleboy/ssh-action` → `azure/k8s-set-context` + `kubectl apply -k deploy/k8s` + `set image sha-<commit>` + `rollout status`. |
| Secretos | `KUBE_CONFIG`, `REGISTRY_USERNAME` y `REGISTRY_PASSWORD`; los tokens de la app viven en el Secret `house-finder-secrets` del clúster. |
| N100 | `deploy/n100` se conserva como alternativa manual; el workflow ya no lo usa y los secretos `N100_*` quedan sin uso. |

Validación: `actionlint` limpio, `kubectl kustomize deploy/k8s` renderiza la imagen GHCR,
suite local en verde; el job de deploy queda pendiente de `KUBE_CONFIG`.
