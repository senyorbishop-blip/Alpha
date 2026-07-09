# syntax=docker/dockerfile:1
# =============================================================================
# Tavern Tabletop — production-ish container image
# Targets Python 3.12 (matches CI). Runtime data lives on a mounted volume at
# /data via DND_DATA_DIR, so the DB/maps/backups survive container rebuilds.
# TTS extras (requirements_tts.txt: torch, etc.) are intentionally NOT installed
# here — they're heavy and optional. Add a separate stage if you need them.
# =============================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DND_DATA_DIR=/data \
    PORT=8000

# curl is only needed for the container HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements change.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
 && pip install -r requirements.txt

# Copy the application source (see .dockerignore for what's excluded).
COPY . .

# Run as a non-root user and give it ownership of the app + data volume.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000
VOLUME ["/data"]

# Liveness probe baked into the image; orchestrators can use it directly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# Serve via uvicorn. PORT is overridable at runtime. WebSocket
# per-message-deflate is pinned on explicitly (repetitive sync JSON
# compresses ~5-10x on the wire).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --ws-per-message-deflate true"]
