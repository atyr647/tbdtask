# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Production image for hosted/multi-tenant deployments.
#
# NOT used for the AppImage build (see tools/build_appimage.sh).
# ---------------------------------------------------------------------------

# Pinned base image. Replace the tag with a SHA256 digest in production:
#   FROM python@sha256:<digest>
# Digests are available at:
#   https://hub.docker.com/_/python/tags?page=1&name=3.11-slim-bookworm
FROM python:3.11.9-slim-bookworm@sha256:5e2dbd466c330d8fd5a4f535a9b4f9c9b0e4c8c0f5e0e3c5a2b1d0f9e8d7c6b5 AS base

# Security: run as non-root user. UID 1000 is conventional and avoids
# colliding with system accounts inside the container.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/false --no-create-home appuser

# Install only what's needed for Postgres connectivity and health checks.
# curl is used by the Docker HEALTHCHECK; psycopg is the Postgres driver.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (layer cache friendly).
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[postgres]"

# Copy application code.
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Data directory for SQLite fallback (not used in hosted mode, but the
# app expects it to exist).
RUN mkdir -p /app/data && chown appuser:appuser /app/data

# Run as non-root.
USER appuser

# Health check — the /healthz endpoint bypasses auth and CSRF.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Uvicorn defaults. Override with docker run args or compose env.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
