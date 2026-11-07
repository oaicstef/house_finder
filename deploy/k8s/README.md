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

## 1. Construir la imagen para ARM

```bash
docker buildx build --platform linux/arm64 \
  -t ghcr.io/<org>/house-finder:0.1.0 --push .
```

Si construyes en una máquina ARM (p. ej. un Mac Apple Silicon o una instancia Ampere) basta:

```bash
docker build -t ghcr.io/<org>/house-finder:0.1.0 .
docker push ghcr.io/<org>/house-finder:0.1.0
```

Ajusta `images[].newTag` en `kustomization.yaml` (o `image:` en `deployment.yaml`).

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

## Notas operativas

| Tema | Detalle |
|---|---|
| Arquitectura | `nodeSelector: kubernetes.io/arch: arm64`; la imagen `python:3.13-slim` es multi-arch. |
| Réplicas | Mantén `replicas: 1` y `strategy: Recreate`: dos pods ejecutarían el cron dos veces. |
| Persistencia | PVC `ReadWriteOnce` de 1 Gi. `fsGroup: 10001` permite escribir al usuario no root. |
| Imágenes de anuncios | La UI enlaza las URLs del portal; no se almacenan binarios. Si el portal bloquea el hotlink, la tarjeta muestra «Sin foto». |
| Sin autenticación | La API y la UI no tienen login: expón el Service solo en redes de confianza. |
| Recursos | 100m/256Mi de request y 1 CPU/768Mi de límite; ajusta según el volumen de anuncios. |
