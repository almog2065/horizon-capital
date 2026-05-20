# syntax=docker/dockerfile:1.7
#
# Horizon Capital — multi-stage production image.
#
# Stages:
#   base    -- shared OS + python deps (compiled layer cache pivot)
#   builder -- wheels into /wheels (no compiler in final image)
#   runtime -- minimal image, non-root user, hardcoded healthcheck
#
# Build once, run as either web, worker, or scheduler via the CMD
# override (see docker-compose.yml).

ARG PYTHON_VERSION=3.12

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build deps for native wheels (numpy etc.). Removed from runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip wheel \
 && pip wheel --wheel-dir=/wheels -r requirements.txt

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/home/horizon/.local/bin:${PATH}" \
    APP_ENV=prod \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8000

# tini gives us proper signal forwarding + zombie reaping
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 horizon \
    && useradd  --system --uid 1000 --gid horizon --create-home --shell /bin/bash horizon

# install pre-built wheels from builder
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl \
 && rm -rf /wheels

WORKDIR /app
COPY --chown=horizon:horizon . /app

# Pre-create writable dirs the app will use at runtime.
RUN mkdir -p /app/artifacts /app/data \
 && chown -R horizon:horizon /app

USER horizon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${HTTP_PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default: API tier. docker-compose overrides this for worker/scheduler.
CMD ["uvicorn", "app.api.app_factory:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
