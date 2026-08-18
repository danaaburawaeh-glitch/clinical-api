# =====================================================================
# Clinical Evidence Gateway — production image
# =====================================================================
# Multi-stage: dependencies are built in a throwaway stage so the final
# image carries no compilers and no build cache.
# =====================================================================

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Wheels are built here so the runtime stage needs no toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ---------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is needed only for the container HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser openapi.yaml pytest.ini ./
COPY --chown=appuser:appuser scripts ./scripts

# Cache directory must be writable by the unprivileged user.
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

# Never run as root: this service fetches remote documents.
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Single worker by default. The in-process rate limiter and SQLite cache
# are per-process; see README "Known limitations" before scaling workers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
