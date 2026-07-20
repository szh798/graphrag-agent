# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG LIGHTRAG_VERSION=1.5.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LIGHTRAG_VERSION=${LIGHTRAG_VERSION} \
    LIGHTRAG_STRICT_VERSION=true \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKING_DIR=/var/lib/lightrag/rag_storage \
    INPUT_DIR=/var/lib/lightrag/inputs \
    LOG_DIR=/var/log/lightrag \
    PYTHONPATH=/app/backend

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       curl \
       libcairo2 \
       libffi8 \
       libgl1 \
       libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/lightrag/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.lock \
    && python -c "import importlib.metadata as m; expected='1.5.4'; actual=m.version('lightrag-hku'); assert actual == expected, f'LightRAG version mismatch: {actual} != {expected}'"

# Copy only runtime Python sources. This deliberately excludes backend/.env,
# local data, uploads, caches, tests, and any developer credentials.
COPY backend/*.py /app/backend/
COPY backend/lightrag_integration /app/backend/lightrag_integration
COPY backend/models /app/backend/models
COPY backend/pipeline /app/backend/pipeline
COPY backend/routers /app/backend/routers
COPY backend/scripts /app/backend/scripts
COPY backend/services /app/backend/services
COPY backend/storage /app/backend/storage

RUN mkdir -p "${WORKING_DIR}" "${INPUT_DIR}" "${LOG_DIR}" \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app /var/lib/lightrag /var/log/lightrag

USER app
WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
  CMD curl --fail --silent "http://127.0.0.1:${PORT}/live" >/dev/null || exit 1

# Railway API service uses this default. The independently scaled worker uses
# the start command from railway-worker.toml with the exact same image.
CMD ["sh", "-c", "exec uvicorn lightrag_integration.runtime:app --host 0.0.0.0 --port ${PORT:-8000}"]
