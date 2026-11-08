# Despliegue en el N100 (Docker Compose)

> **Alternativa.** El pipeline despliega en **Kubernetes ARM64** (`deploy/k8s`). Este
> directorio conserva el despliegue en el N100 para uso manual; `deploy.sh` sigue
> funcionando pero ningún workflow lo invoca.

Destino por defecto del pipeline: el servidor **N100** (x86_64), ejecutando la imagen
publicada en GHCR con Docker Compose. Sustituye al despliegue en Kubernetes; los
manifiestos de [`deploy/k8s`](../k8s/README.md) se mantienen como alternativa.

```
deploy/n100/
├── docker-compose.yml   # servicio house-finder + healthcheck + volumen ./data
├── .env.example         # configuración y tokens (copiar a .env, nunca a git)
├── deploy.sh            # pull + recreate + espera a healthy (lo ejecuta el workflow)
└── README.md
```

## Cómo desplegar (manual)

El workflow ya no despliega en el N100. Para hacerlo a mano, construye/publica la imagen
`linux/amd64` y ejecuta por SSH:

1. `git fetch && git pull --ff-only` en el checkout del N100 (`N100_DEPLOY_PATH`).
2. `deploy/n100/deploy.sh sha-<commit>`: `docker compose pull` + `up -d` + espera a healthy.

Si el contenedor no llega a `healthy` en 60 s, el job falla y muestra los últimos logs.
El contenedor anterior se reemplaza solo cuando la nueva imagen arranca correctamente
(`docker compose up -d` recrea el servicio; el healthcheck falla el despliegue).

Si el paquete de GHCR es privado y no hay credenciales (`REGISTRY_*`), el `pull` falla y
`deploy.sh` construye la imagen **desde el checkout del N100** (mismo enfoque que el API de
Padel: `docker compose build`). En ese caso no necesitas secretos de registro.

## Configuración inicial (una sola vez)

En el N100:

```bash
git clone https://github.com/oaicstef/house_finder.git ~/house_finder
cd ~/house_finder/deploy/n100
cp .env.example .env
nano .env          # SEARCH_PROFILE_JSON, HOST_PORT...
chmod +x deploy.sh
docker compose up -d
```

Comprueba: `curl -s http://127.0.0.1:8000/health` → `{"status":"ok",...}`.
La UI queda en `http://<n100>:8000/`.

> El contenedor corre como UID 10001. `deploy.sh` crea `data/` y le da permisos de
> escritura (`chmod 0777`); si prefieres algo más estricto, usa `chown 10001:10001`.

Los tokens `APIFY_TOKEN` y `DEEPSEEK_API_KEY` se guardan como secretos del repositorio y
`deploy.sh` los escribe en `.env` (permisos 600) en cada despliegue; no hace falta
copiarlos a mano en el N100.

## Secretos del repositorio

Settings → Secrets and variables → Actions:

| Secreto | Obligatorio | Uso |
|---|---|---|
| `N100_HOST` | sí | IP o host del N100 (p. ej. `192.168.0.63`). |
| `N100_USER` | sí | Usuario SSH con acceso a Docker. |
| `N100_PASSWORD` | sí | Contraseña SSH del usuario del N100. |
| `N100_SSH_KEY` | alternativa | Clave privada SSH sin passphrase (si no usas contraseña). |
| `N100_PORT` | no | Puerto SSH (por defecto 22). |
| `N100_DEPLOY_PATH` | no | Checkout del repo (por defecto `$HOME/house_finder`). |
| `APIFY_TOKEN` | sí | Token de Apify; `deploy.sh` lo inyecta en `.env`. |
| `DEEPSEEK_API_KEY` | sí | Clave del LLM; `deploy.sh` la inyecta en `.env`. |
| `REGISTRY_USERNAME` | si el paquete es privado | Usuario GHCR (`oaicstef`). |
| `REGISTRY_PASSWORD` | si el paquete es privado | PAT con `read:packages`. |

Sin credenciales de registro, `deploy.sh` omite `docker login` y asume que el N100 ya
está autenticado o que el paquete es público.

## Operación

```bash
cd ~/house_finder/deploy/n100
docker compose ps
docker compose logs -f --tail=100 house-finder
docker compose restart house-finder
```

- **Puerto**: `HOST_PORT` en `.env` (por defecto 8000). Si el N100 ya usa 8000, cambia
  también la regla del proxy que apunte a la UI.
- **Datos**: `./data` en el N100 (resultados JSON). Se conserva entre despliegues.
- **Scheduler**: una sola instancia; no escales el servicio (`replicas`/`scale > 1`
  duplicaría el job diario de las 07:00).
- **Config**: editar `.env` y `docker compose up -d` (recrea el contenedor con la misma
  imagen). No hace falta rebuild.
