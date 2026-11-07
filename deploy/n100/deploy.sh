#!/usr/bin/env bash
# Despliegue en el N100: pull de la imagen publicada y recreación del contenedor.
# Uso: ./deploy.sh <tag>   (p. ej. sha-280200e; por defecto latest)
set -euo pipefail

IMAGE_TAG="${1:-latest}"
COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$COMPOSE_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Creado .env desde .env.example"
fi

upsert_env() {
  local key="$1" value="$2" tmp
  [ -n "$value" ] || return 0
  tmp="$(mktemp)"
  grep -v "^${key}=" .env > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" .env
  chmod 600 .env
}

# Tokens inyectados por el workflow (secrets del repo) sin dejarlos en logs.
upsert_env APIFY_TOKEN "${APIFY_TOKEN:-}"
upsert_env DEEPSEEK_API_KEY "${DEEPSEEK_API_KEY:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker no está instalado o no está en PATH." >&2
  exit 1
fi

# El contenedor corre como UID 10001: el bind mount debe ser escribible.
mkdir -p data/results
chmod 0777 data data/results

# Solo si el paquete de GHCR es privado (secretos REGISTRY_* del workflow).
if [ -n "${REGISTRY_PASSWORD:-}" ]; then
  echo "$REGISTRY_PASSWORD" | docker login ghcr.io -u "${REGISTRY_USERNAME:?falta REGISTRY_USERNAME}" --password-stdin
fi

echo "Desplegando house-finder:${IMAGE_TAG} en $COMPOSE_DIR"
if ! IMAGE_TAG="$IMAGE_TAG" docker compose pull house-finder; then
  echo "No se pudo descargar la imagen; construyendo desde el checkout..." >&2
  IMAGE_TAG="$IMAGE_TAG" docker compose build house-finder
fi
IMAGE_TAG="$IMAGE_TAG" docker compose up -d --remove-orphans

echo "Esperando a que el contenedor esté healthy..."
status="unknown"
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' house-finder 2>/dev/null || echo missing)"
  [ "$status" = "healthy" ] && break
  sleep 2
done

if [ "$status" != "healthy" ]; then
  echo "El contenedor no llegó a healthy (estado: $status). Últimos logs:" >&2
  IMAGE_TAG="$IMAGE_TAG" docker compose logs --tail=50 house-finder >&2
  exit 1
fi

docker image prune -f >/dev/null
echo "house-finder ${IMAGE_TAG} desplegado y healthy."
