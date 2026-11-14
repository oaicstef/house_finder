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
├── ingress.yaml          # https://oaichousefinder.duckdns.org + TLS cert-manager
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
  --from-literal=IDEALISTA_API_CLIENT_ID='...' \
  --from-literal=IDEALISTA_API_CLIENT_SECRET='...' \
  --from-literal=DEEPSEEK_API_KEY='sk-...' \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Alertas push (ntfy)

`ALERTS_ENABLED` está activado en el ConfigMap (`deploy/k8s/configmap.yaml`), así que el
topic es obligatorio: con alertas activas y sin `NTFY_TOPIC` el pod **no arranca**
(`CONFIGURATION_ERROR`) y el rollout falla, para que las notificaciones no se pierdan en
silencio.

El topic es un secreto: **no se versiona**. Se guarda en el repositorio de GitHub
(*Settings → Secrets and variables → Actions*):

| Secreto del repositorio | Obligatorio | Uso |
|---|---|---|
| `NTFY_TOPIC` | sí (con alertas activas) | Topic al que está suscrita la app del móvil. |
| `NTFY_TOKEN` | no | Token `Bearer`, solo si el topic está protegido en el servidor ntfy. |

En cada despliegue el pipeline escribe esos valores en el Secret `house-finder-secrets`
con `kubectl patch --type merge`, de modo que **no borra** `APIFY_TOKEN`,
`DEEPSEEK_API_KEY` ni el resto. No hace falta ningún `kubectl` manual: basta con
`git push` a `main` (o lanzar el workflow a mano con *Run workflow*).

Si `ALERTS_ENABLED` vuelve a `"false"`, el paso se salta y no toca los secretos. Si está
a `"true"` y falta `NTFY_TOPIC`, el pipeline se detiene **antes** de aplicar los manifiestos
con un error explícito.

Instala la app **ntfy** en el móvil (App Store / Google Play) y suscríbete al mismo topic.
Ojo: ntfy distingue **mayúsculas de minúsculas**; copia el topic, no lo teclees.
`ALERT_CLICK_URL` ya apunta a la UI publicada.

Para rotar el topic: cambia el secreto del repositorio y vuelve a desplegar (o
`kubectl -n house-finder rollout restart deployment/house-finder`).

Las credenciales `IDEALISTA_API_*` solo hacen falta si Idealista aprobó el acceso a su API
oficial y se selecciona `idealista=idealista-api` en `SCRAPER_PROVIDERS`. No habilites ese
proveedor hasta validar su acceso y respuesta en el entorno autorizado.

## 3. Aplicar

```bash
kubectl apply -k deploy/k8s
kubectl -n house-finder rollout status deploy/house-finder
```

## 4. Acceder a la UI

El Service es `ClusterIP`; el Ingress [`ingress.yaml`](ingress.yaml) lo publica en
`https://oaichousefinder.duckdns.org` con certificado de `cert-manager`. Requisitos:

| Requisito | Detalle |
|---|---|
| DNS | El registro A de `oaichousefinder.duckdns.org` debe apuntar a la IP pública del clúster (la del `ingress-nginx`, hoy `204.216.209.9`). |
| Ingress controller | `ingressClassName: nginx`, con el `ClusterIssuer` `letsencrypt-prod` (solo HTTP-01: no admite proxy intermedio ni DNS-01). |
| Certificado | El primer `apply` crea la orden ACME y el Secret `house-finder-duckdns-tls`; el TLS del Ingress queda listo cuando la orden pasa a `Ready`. |

Actualizar el registro de DuckDNS (el token no está en el repo):

```bash
curl "https://www.duckdns.org/update?domains=oaichousefinder&token=$DUCKDNS_TOKEN&ip=204.216.209.9"
dig +short oaichousefinder.duckdns.org   # debe devolver 204.216.209.9
```

Seguimiento del certificado:

```bash
kubectl -n house-finder get ingress house-finder
kubectl -n house-finder get certificate,order,challenge
kubectl -n house-finder describe certificate house-finder-duckdns-tls
```

Si la orden falla con `wrong status code '404'` o timeouts en el `challenge`, el DNS sigue
apuntando a otro sitio (p. ej. el Nginx Proxy Manager de casa, que no puede resolver el
HTTP-01 del clúster). El Ingress está bien: hay que corregir el registro A.

También puedes seguir usando port-forward para depurar sin pasar por el Ingress:

```bash
kubectl -n house-finder port-forward svc/house-finder 8000:80
# UI:   http://127.0.0.1:8000/
# Docs: http://127.0.0.1:8000/docs
# Salud: http://127.0.0.1:8000/health
```

## 5. Cambiar la búsqueda sin rebuild

Desde la UI (**Editar criterios → Guardar criterios**) o con la API:

```bash
curl -X PUT https://oaichousefinder.duckdns.org/api/v1/search-profile \
  -H "Content-Type: application/json" \
  -d '{"location":"Madrid","portals":["idealista","fotocasa"],"postal_code":"28031","radius_km":5,"max_price_eur":900000,"min_size_m2":140,"min_rooms":4,"min_year_built":2020,"must_have":["padel"]}'

curl -X DELETE https://oaichousefinder.duckdns.org/api/v1/search-profile  # restaurar
```

El perfil se guarda en `/data/search-profile.json` (PVC), así que sobrevive a los rollouts y
lo comparten todos los dispositivos. `SEARCH_PROFILE_JSON` sigue siendo el valor por defecto
que restaura el `DELETE`.

También puedes editar `SEARCH_PROFILE_JSON` en `configmap.yaml` (o con
`kubectl -n house-finder edit configmap house-finder-config`) y reiniciar:

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
| Perfil editable | La UI guarda `search-profile.json` en `/data` (mismo PVC); el cron lo lee antes de cada ejecución. |
| Raíz de solo lectura | `readOnlyRootFilesystem: true`: la app únicamente escribe en `/data` (PVC). |
| Imagen endurecida | El Dockerfile aplica `apt-get upgrade` y elimina `pip` tras instalar dependencias (sin CVEs vendorizados y sin herramientas de instalación en producción). |
| Imágenes de anuncios | La UI enlaza las URLs del portal; no se almacenan binarios. Si el portal bloquea el hotlink, la tarjeta muestra «Sin foto». |
| Sin autenticación | La API y la UI no tienen login: expón el Service solo en redes de confianza. |
| Ingress | `ingress.yaml` publica `https://oaichousefinder.duckdns.org`; el timeout de nginx sube a 660 s porque `POST /api/v1/search-profile/run` es síncrono y puede tardar varios minutos. |
| Recursos | 100m/256Mi de request y 1 CPU/768Mi de límite; ajusta según el volumen de anuncios. |
