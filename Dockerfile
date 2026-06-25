# syntax=docker/dockerfile:1.7

# ---- builder ---------------------------------------------------------------
# Resolve and sync dependencies in an isolated layer so source code changes
# don't bust the dependency cache.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Copy only the lock-related metadata first to maximise Docker layer caching.
COPY pyproject.toml uv.lock ./

# Install production dependencies into a project-local virtualenv.
# --frozen pins to uv.lock; --no-install-project keeps the image lean until
# the source is copied in the next stage.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---- runtime ---------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src

# Trust the MinCifry (Минцифры / Russian Ministry of Digital Development) CA
# so the new MAX API domain platform-api2.max.ru (migration deadline
# 2026-07-19) is reachable. Its TLS chain is signed by the Russian Trusted
# CA, which is not in the default Mozilla bundle shipped in this slim image.
# See issue #233 and
# https://docs.lanbilling.ru/52/integration/sber/install_sertificates_mincifry/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && install -d /usr/local/share/ca-certificates \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_root_ca_pem.crt \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_sub_ca_pem.crt \
    && update-ca-certificates

# Point Python HTTP clients at the OS trust store — they default to certifi
# and ignore update-ca-certificates otherwise (requests, httpx, aiohttp, ...).
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Create a non-root user for runtime. The UID matches the default in the
# official python images for predictable local bind-mount permissions.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the prepared virtualenv from the builder stage.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Copy the project source. The .dockerignore file keeps test/cache data out
# of the build context.
COPY --chown=app:app pyproject.toml uv.lock ./
COPY --chown=app:app src ./src
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./alembic.ini

USER app

EXPOSE 8000

# Default to the FastAPI entrypoint. Override with `docker compose run api ...`
# for one-off tasks such as Alembic migrations or smoke checks.
CMD ["uvicorn", "apply_pilot.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
