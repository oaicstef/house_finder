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

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
