# Imagen multi-arquitectura (amd64 + arm64). Para ARM:
#   docker buildx build --platform linux/arm64 -t house-finder:latest .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Usuario sin privilegios (UID/GID fijos para que el PVC pueda montarse con fsGroup).
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app

# Actualiza los paquetes Debian con parche disponible: Trivy falla el pipeline si
# queda un CVE HIGH/CRITICAL corregible en la base (p. ej. zlib1g).
RUN apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# Se elimina pip (y sus dependencias vendorizadas: urllib3, msgpack, setuptools)
# porque en ejecución no se instala nada; así la imagen no arrastra CVEs de las
# librerías internas de pip que el escáner de Trivy reporta.
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip \
              /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13

COPY app ./app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
