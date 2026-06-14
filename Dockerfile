# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the ApplyPilot runtime image.
#
# Stage 1 ("builder") resolves and installs the project into a uv-managed
# virtualenv using the same Python version that ships in the runtime base.
#
# Stage 2 ("runtime") copies the prebuilt venv and the application source
# into a slim Debian image. The default CMD runs a tiny placeholder HTTP
# server that responds 200 OK on `/healthz`; it will be replaced by
# `uvicorn job_apply.main:app` once the FastAPI app factory lands (M1+).
# The `bot`, `scheduler`, and `worker` services override `command` in
# docker-compose.yml to run their own placeholders until their entrypoints
# are implemented in later milestones.

# ---------- Stage 1: dependency + project install ----------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# Keep uv deterministic: copy instead of hardlink, precompile bytecode,
# never download a Python interpreter at sync time.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install only the project metadata first so the dependency layer is
# cached and reused on pure source changes.
COPY pyproject.toml uv.lock ./

# Resolve and install runtime dependencies without the project itself.
# `--no-install-project` keeps this layer fast and stable.
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the application source and install the project into the venv.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini pyproject.toml ./

RUN uv sync --frozen --no-dev


# ---------- Stage 2: lean runtime image ----------
FROM python:3.13-slim AS runtime

# Unbuffered I/O for container logs, src/ on PYTHONPATH so `python -m
# job_apply` resolves, the venv first on PATH, and a default API port.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PORT=8000

WORKDIR /app

# Create a non-root user to run the application. UID/GID 1000 matches
# the typical host user so bind-mounted volumes stay writable.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app \
       --no-create-home --shell /usr/sbin/nologin app

# Copy the prebuilt virtualenv and the application source from the builder.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/alembic /app/alembic
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/pyproject.toml /app/pyproject.toml

# Ensure the shared runtime data directory exists and is owned by `app`.
RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8000

# Placeholder HTTP server: returns 200 on /healthz, 404 elsewhere.
# Replaced by `uvicorn job_apply.main:app --host 0.0.0.0 --port ${PORT}`
# once the FastAPI app factory lands in M1+.
CMD ["python", "-u", "-c", "from http.server import HTTPServer, BaseHTTPRequestHandler\n\nclass _Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        if self.path == '/healthz':\n            self.send_response(200)\n            self.send_header('Content-Type', 'text/plain')\n            self.end_headers()\n            self.wfile.write(b'ok')\n        else:\n            self.send_response(404)\n            self.end_headers()\n\n    def log_message(self, *args, **kwargs):\n        return\n\nHTTPServer(('0.0.0.0', 8000), _Handler).serve_forever()\n"]

# Liveness check: hit /healthz on the local port. The placeholder server
# above returns 200 on /healthz; the real API will serve the same path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -u -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz').read()" || exit 1
