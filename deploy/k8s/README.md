# Despliegue en Kubernetes (ARM64)

Manifiestos para ejecutar el agente en un clúster **arm64** con la UI incluida, el job
diario y una única réplica (el scheduler vive dentro del proceso).

```
deploy/k8s/
├── namespace.yaml        # namespace house-finder
├── configmap.yaml        # entorno + SEARCH_PROFILE_JSON (perfil único de búsqueda)
├── secret.example.yaml   # plantilla de tokens (NO aplicar con valores reales en git)
├── pvc.yaml              # volumen de resultados (RESULTS_DIR=/data/results)
├── deployment.yaml       # pod ARM64, réplica única, probes y límites
├── service.yaml          # ClusterIP :80 -> :8000
└── kustomization.yaml    # kubectl apply -k deploy/k8s
```

## 1. Imagen: la publica el pipeline

El workflow `.github/workflows/release.yml` construye la imagen `linux/arm64` y la publica
en GHCR en cada push a `main` y en cada tag `v*`:

```
ghcr.io/oaicstef/house-finder:latest
ghcr.io/oaicstef/house-finder:latest-arm64
ghcr.io/oaicstef/house-finder:<rama-o-tag>
ghcr.io/oaicstef/house-finder:sha-<commit>
```

Después aplica estos manifiestos y fija el Deployment al tag `sha-<commit>`. Construir a
mano solo hace falta para probar cambios sin publicar:

```bash
docker buildx build --platform linux/arm64 \
  -t ghcr.io/oaicstef/house-finder:0.1.0 --push .
```

Si construyes en una máquina ARM (p. ej. un Mac Apple Silicon o una instancia Ampere) basta:

```bash
docker build -t ghcr.io/oaicstef/house-finder:0.1.0 .
docker push ghcr.io/oaicstef/house-finder:0.1.0
```

Ajusta `images[].newTag` en `kustomization.yaml` (o el tag con `kubectl set image`).

## 2. Crear el Secret (tokens)

```bash
kubectl create namespace house-finder --dry-run=client -o yaml | kubectl apply -f -
kubectl -n house-finder create secret generic house-finder-secrets \
  --from-literal=APIFY_TOKEN='apify_api_...' \
  --from-literal=DEEPSEEK_API_KEY='sk-...' \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 3. Aplicar

```bash
kubectl apply -k deploy/k8s
kubectl -n house-finder rollout status deploy/house-finder
```

## 4. Acceder a la UI

El Service es `ClusterIP`; usa port-forward (o añade tu Ingress si lo necesitas):

```bash
kubectl -n house-finder port-forward svc/house-finder 8000:80
# UI:   http://127.0.0.1:8000/
# Docs: http://127.0.0.1:8000/docs
# Salud: http://127.0.0.1:8000/health
```

## 5. Cambiar la búsqueda sin rebuild

Edita `SEARCH_PROFILE_JSON` en `configmap.yaml` (o con
`kubectl -n house-finder edit configmap house-finder-config`) y reinicia:

```bash
kubectl -n house-finder rollout restart deploy/house-finder
```

El mismo perfil lo usan:

- el job diario de las 07:00 (`TIMEZONE`),
- el botón **Ejecutar búsqueda** de la UI,
- `POST /api/v1/search-profile/run`.

Si el JSON es inválido, el pod no arranca y `kubectl logs` muestra el campo culpable
(`CONFIGURATION_ERROR`).

## CI/CD (GitHub Actions)

| Workflow | Disparador | Función |
|---|---|---|
| `.github/workflows/coverage.yml` | PR y push a `main` | `pytest --cov`, resumen en el job y artefacto HTML/XML. |
| `.github/workflows/security.yml` | PR, push a `main`, lunes 06:00 UTC | `pip-audit` + Trivy (fs/IaC e imagen), gate HIGH/CRITICAL. |
| `.github/workflows/release.yml` | push a `main`, tags `v*`, manual | Build arm64 → GHCR → `kubectl apply -k` → imagen `sha-<commit>` → rollout. Con tag `v*`, GitHub Release. |

### Secretos necesarios

| Secreto | Contenido |
|---|---|
| `KUBE_CONFIG` | Kubeconfig con permisos sobre `house-finder`. |
| `REGISTRY_USERNAME` | Usuario GHCR (p. ej. `oaicstef`). |
| `REGISTRY_PASSWORD` | PAT con `read:packages`. |

El workflow crea el namespace si falta y el secret de pull `ghcr-login-secret` solo la
primera vez:

```bash
kubectl -n house-finder create secret docker-registry ghcr-login-secret \
  --docker-server=ghcr.io \
  --docker-username="$REGISTRY_USERNAME" \
  --docker-password="$REGISTRY_PASSWORD" \
  --docker-email="docker@github.com"
```

Si el paquete de GHCR es público, el pull secret no es imprescindible, pero se mantiene
para que el despliegue funcione también con el paquete privado.

### Despliegue manual

```bash
kubectl apply -k deploy/k8s
kubectl -n house-finder set image deployment/house-finder \
  api=ghcr.io/oaicstef/house-finder:<tag>
kubectl -n house-finder rollout status deployment/house-finder --timeout=180s
```

## Notas operativas

| Tema | Detalle |
|---|---|
| Arquitectura | `nodeSelector: kubernetes.io/arch: arm64`; la imagen `python:3.13-slim` es multi-arch. |
| Réplicas | Mantén `replicas: 1` y `strategy: Recreate`: dos pods ejecutarían el cron dos veces. |
| Persistencia | PVC `ReadWriteOnce` de 1 Gi. `fsGroup: 10001` permite escribir al usuario no root. |
| Raíz de solo lectura | `readOnlyRootFilesystem: true`: la app únicamente escribe en `/data` (PVC). |
| Imagen endurecida | El Dockerfile aplica `apt-get upgrade` y elimina `pip` tras instalar dependencias (sin CVEs vendorizados y sin herramientas de instalación en producción). |
| Imágenes de anuncios | La UI enlaza las URLs del portal; no se almacenan binarios. Si el portal bloquea el hotlink, la tarjeta muestra «Sin foto». |
| Sin autenticación | La API y la UI no tienen login: expón el Service solo en redes de confianza. |
| Recursos | 100m/256Mi de request y 1 CPU/768Mi de límite; ajusta según el volumen de anuncios. |
