# Infraestructura

Mapa de dónde corre cada cosa y cómo se despliega. Léelo antes de tocar el despliegue,
los secretos o el clúster.

## 1. Mapa

```
GitHub  oaicstef/house_finder
   │  push a main  ·  tag v*  ·  workflow_dispatch
   ▼
GitHub Actions  "Build and Deploy — house_finder"
   ├─ imagen linux/arm64  →  ghcr.io/oaicstef/house-finder:latest / :sha-xxxxxxx
   └─ kubectl (secreto KUBE_CONFIG)
        ▼
   Clúster Kubernetes remoto  ·  Oracle Cloud (AS31898), Siziano (Lombardía, IT)
   IP pública 204.216.209.9  ·  instancia Ampere ARM64
        ├─ namespace        house-finder
        ├─ deployment       house-finder (1 réplica, contenedor "api", :8000)
        ├─ service          house-finder:80 → 8000
        ├─ ingress          nginx + cert-manager (letsencrypt-prod)
        ├─ PVC              resultados JSON montados en /data  (RESULTS_DIR=/data/results)
        └─ ConfigMap        house-finder-config   (envFrom)
           Secret           house-finder-secrets  (envFrom)
        ▼
   https://oaichousefinder.duckdns.org   (registro A → 204.216.209.9)
```

**La aplicación NO corre en casa.** El clúster está en Oracle Cloud; una caída de la red
doméstica o del N100 tumba el copilot web, pero no house_finder.

Contexto de casa (para no confundirlos):

| Equipo | IP | Qué corre ahí |
|---|---|---|
| `rpi-mad` (Raspberry Pi 5) | 192.168.0.156 | Checkouts de `~/src/` y `copilot-web` (nginx 7681 → ttyd 127.0.0.1:7682) |
| N100 (Ubuntu) | 192.168.0.63 | **nginx-proxy-manager** (proxy inverso y TLS de los hosts públicos, UI :81), **`immich-server`** (:2283) y **`padelapi`** (Docker Compose) |
| Router | 192.168.0.1 | Salida a Internet; IP pública 79.117.16.157 |

El clúster de Oracle es **compartido**: además de `house_finder` aloja **`immich-ml`**
(machine learning de Immich). El servidor de Immich vive en el N100 y el ML habla con él
por **mTLS**, así que un cambio de certificados, URL o nombre de servicio en cualquiera de
los dos lados rompe búsqueda inteligente y reconocimiento facial aunque la UI siga
funcionando. `padelapi` se movió a casa: su deployment en k8s debe quedarse en
`replicas: 0`.


## 2. Pipeline (`.github/workflows/release.yml`)

Se dispara con **push a `main`**, **tags `v*`** y ejecución manual. `concurrency` no
cancela despliegues a medias.

| Paso | Qué hace |
|---|---|
| Build & Push Image | Buildx + QEMU, **solo `linux/arm64`**, publica `:latest`, `:latest-arm64`, `:ref_tag` y el tag inmutable `:sha-<7>` |
| Set up Kubeconfig | `azure/k8s-set-context` con el secreto `KUBE_CONFIG` |
| Ensure namespace and GHCR pull secret | Crea el namespace y `ghcr-login-secret` si faltan |
| Sync alert credentials | Copia `NTFY_TOPIC`/`NTFY_TOKEN` del repositorio al Secret del clúster con `patch --type merge` (no borra las demás claves); se salta si `ALERTS_ENABLED` no es `true`; aborta si hay alertas activas y falta el topic; anota el deployment con un hash del topic para reiniciar solo cuando cambia |
| Apply Kubernetes manifests | `kubectl apply -k deploy/k8s` |
| Pin deployment to the built image | `kubectl set image ... :sha-<7>` (tag inmutable, sin caché de nodo) |
| Wait for rollout | `kubectl rollout status --timeout=180s` |
| Publish GitHub Release | Solo con tags `v*` |

Otros workflows: `coverage.yml` (pytest + cobertura) y `security.yml` (pip-audit,
trivy/escaneo de imagen).

**La imagen es arm64.** No puede ejecutarse en nodos amd64: si el clúster cambia de
arquitectura hay que añadir `linux/amd64` en `platforms`.

## 3. Secretos del repositorio (GitHub → Settings → Secrets → Actions)

| Secreto | Uso |
|---|---|
| `KUBE_CONFIG` | Kubeconfig del clúster de Oracle. Sin él no hay despliegue. |
| `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` | Pull de `ghcr.io` (secret `ghcr-login-secret`). |
| `APIFY_TOKEN` | Scraping (solo pinta si el secreto del clúster no lo tiene). |
| `DEEPSEEK_API_KEY` | Clasificación LLM. |
| `NTFY_TOPIC` | Topic de las alertas push; el pipeline lo escribe en `house-finder-secrets`. |
| `NTFY_TOKEN` | Opcional, si el topic está protegido en el servidor ntfy. |
| `N100_HOST`, `N100_USER`, `N100_PASSWORD`, `N100_PORT` | **Sin uso** (restos del despliegue por Docker Compose). |

El Secret del clúster `house-finder-secrets` (creado a mano la primera vez, ver
`deploy/k8s/README.md`) debe existir con `APIFY_TOKEN`, `DEEPSEEK_API_KEY` y, si aplica,
`IDEALISTA_API_*`. El pipeline **solo añade** las claves de alertas.

## 4. Configuración de la aplicación

Se inyecta con `envFrom` (ConfigMap + Secret): **cualquier clave nueva en el ConfigMap
llega al pod sin tocar el `deployment.yaml`**.

Fijado en el ConfigMap (`deploy/k8s/configmap.yaml`):

| Clave | Valor | Nota |
|---|---|---|
| `ENABLE_SCHEDULER` | `true` | Job diario a las 07:00 (`TIMEZONE=Europe/Madrid`). |
| `SCRAPER_PROVIDERS` | `idealista=apify,fotocasa=local` | `idealista-api` solo con acceso aprobado. |
| `RESULTS_DIR` | `/data/results` | PVC: los resultados sobreviven a reinicios. |
| `ALERTS_ENABLED` | `true` | Sin `NTFY_TOPIC` el arranque falla (`CONFIGURATION_ERROR`). |
| `ALERT_CLICK_URL` | `https://oaichousefinder.duckdns.org` | URL que abre la notificación. |

## 5. Verificar tras cualquier cambio

```bash
# 1. El pipeline
gh run list --repo oaicstef/house_finder --limit 3
gh run watch <run-id> --repo oaicstef/house_finder

# 2. El servicio (los campos nuevos prueban que la imagen es la recién construida)
curl -s https://oaichousefinder.duckdns.org/health
# {"status":"ok","scheduler_enabled":true,"scheduler_running":true,"next_run_time":"..."}

# 3. Alertas: ver el topic y disparar el job
curl -s "https://ntfy.sh/<TOPIC>/json?poll=1&since=all"
curl -X POST https://oaichousefinder.duckdns.org/api/v1/scheduled-searches/run

# 4. Logs del pod (desde una máquina con KUBECONFIG; en la Pi no hay contexto)
kubectl -n house-finder logs deployment/house-finder | grep -E "listing_alert|scheduled_search"
```

`kubectl` **no está configurado en la Pi** (`current-context is not set`): los cambios de
infraestructura se aplican con el pipeline, o desde una máquina con el kubeconfig del
clúster.

## 6. Puntos frágiles (lecciones aprendidas)

- **Topics de ntfy sensibles a mayúsculas**: `casa-nueva-AbC` y `casa-nueva-abc` son
  distintos y el envío responde `200` en ambos. Un topic con distinta capitalización
  parece «no llegar» sin ningún error.
- **`ALERTS_ENABLED=true` sin topic → el pod no arranca**. Por eso el pipeline aborta
  antes de aplicar los manifiestos en vez de dejar un CrashLoopBackOff.
- **Alertas vs. ejecuciones manuales**: solo las ejecuciones `trigger="scheduler"` sirven
  de referencia y de idempotencia; una búsqueda manual del día no cancela el cron.
- **DNS en DuckDNS**: el registro A apunta a la IP de Oracle. Si se recrea la instancia,
  hay que actualizarlo (en casa, `oaiccopilot.duckdns.org` → 79.117.16.157 la gestiona el
  N100).
- **Tag inmutable**: el despliegue fija `:sha-<commit>`; un `apply` sin nuevo commit no
  reinicia nada. Para forzar reinicio: `kubectl rollout restart deployment/house-finder`.
- **PVC de resultados**: el cron diario compara con la última ejecución programada guardada
  en `/data/results`. Si el volumen se pierde, la primera mañana alerta de todo.
