# Despliegue en el N100 (Docker Compose)

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

## Cómo despliega el pipeline

`.github/workflows/release.yml` construye la imagen `linux/amd64`, la publica en GHCR con
el tag `sha-<commit>` y por SSH:

1. `git fetch && git pull --ff-only` en el checkout del N100 (`N100_DEPLOY_PATH`).
2. `deploy/n100/deploy.sh sha-<commit>`: `docker compose pull` + `up -d` + espera a healthy.

Si el contenedor no llega a `healthy` en 60 s, el job falla y muestra los últimos logs.
El contenedor anterior se reemplaza solo cuando la nueva imagen arranca correctamente
(`docker compose up -d` recrea el servicio; el healthcheck falla el despliegue).

## Configuración inicial (una sola vez)

En el N100:

```bash
git clone https://github.com/oaicstef/house_finder.git ~/house_finder
cd ~/house_finder/deploy/n100
cp .env.example .env
nano .env          # APIFY_TOKEN, DEEPSEEK_API_KEY, SEARCH_PROFILE_JSON, HOST_PORT...
chmod +x deploy.sh
docker compose up -d
```

Comprueba: `curl -s http://127.0.0.1:8000/health` → `{"status":"ok",...}`.
La UI queda en `http://<n100>:8000/`.

> El contenedor corre como UID 10001. `deploy.sh` crea `data/` y le da permisos de
> escritura (`chmod 0777`); si prefieres algo más estricto, usa `chown 10001:10001`.

## Secretos del repositorio

Settings → Secrets and variables → Actions:

| Secreto | Obligatorio | Uso |
|---|---|---|
| `N100_HOST` | sí | IP o host del N100 (p. ej. `192.168.0.63`). |
| `N100_USER` | sí | Usuario SSH con acceso a Docker. |
| `N100_SSH_KEY` | sí | Clave privada SSH (sin passphrase; la pública en `~/.ssh/authorized_keys`). |
| `N100_PORT` | no | Puerto SSH (por defecto 22). |
| `N100_DEPLOY_PATH` | no | Checkout del repo (por defecto `$HOME/house_finder`). |
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
