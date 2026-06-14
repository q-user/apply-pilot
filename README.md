# ApplyPilot

Telegram-first assistant for finding, scoring, reviewing, and applying to jobs.

- FastAPI monolith with vertical slices
- Python 3.13, uv, SQLAlchemy 2.x, Alembic
- PostgreSQL + Redis
- Telegram bot, scheduler, and worker run as separate processes
- hh as the first source, designed for additional sources later

## Roadmap

| Milestone | Title                       | Scope              |
| --------- | --------------------------- | ------------------ |
| M0        | Foundation                  | MVP                |
| M1        | Users, Resumes, Telegram    | MVP                |
| M2        | hh Collector                | MVP                |
| M3        | Scoring and Drafts          | MVP                |
| M4        | Telegram Review Loop        | MVP                |
| M5        | Apply Worker MVP            | MVP                |
| M6        | Web/API Dashboard           | Post-MVP           |
| M7        | Additional Sources          | Post-MVP           |
| M8        | Intelligence Improvements   | Post-MVP           |

> **Note:** M1's auth vertical slice evaluates `fastapi-users` (with the
> SQLAlchemy adapter) before falling back to hand-rolled endpoints. See the
> corresponding M1 issue for the decision criteria.

Issue tracking, milestones, and labels are created via `scripts/bootstrap_github.sh`
after the repository exists on GitHub.

## Quick start (local)

```bash
uv sync --extra dev
uv run pytest -v
```

Dev tooling: **ruff** (lint + format) and **ty** for typing. Pytest
runs with `pytest-xdist` (`-n auto`) and a **5s per-test timeout** via
`pytest-timeout`; both are configured in `pyproject.toml` under
`[tool.pytest.ini_options]`.

## Docker

A local stack is provided via `Dockerfile` and `docker-compose.yml`. It
spins up the API alongside PostgreSQL, Redis, and three placeholder
processes (bot, scheduler, worker) that will be filled in by later
milestones.

```bash
# One-time: copy the env template and edit secrets if needed.
cp .env.example .env

# Build the image and start the runtime services.
docker compose up --build postgres redis api
```

The compose file defines six services:

| Service     | Image / build        | Purpose                                                                                          |
| ----------- | -------------------- | ------------------------------------------------------------------------------------------------ |
| `postgres`  | `postgres:16-alpine` | Stateful relational store. Healthchecked via `pg_isready`. Named volume `postgres_data`.        |
| `redis`     | `redis:7-alpine`     | Cache + future broker. Healthchecked via `redis-cli ping`. Named volume `redis_data`.             |
| `api`       | `Dockerfile`         | FastAPI monolith placeholder. Default CMD is a tiny HTTP server that returns 200 on `/healthz`. Replaced by uvicorn in M1+. |
| `bot`       | `Dockerfile`         | **Placeholder** — `command: ["python", "-c", "import time; time.sleep(3600)"]`. Replaced by the Telegram bot entrypoint in M1. |
| `scheduler` | `Dockerfile`         | **Placeholder** — same as `bot`; replaced by the scheduled-job runner in a later milestone.       |
| `worker`    | `Dockerfile`         | **Placeholder** — same as `bot`; replaced by the background apply worker in M5.                   |

Notes:

- The `api`, `bot`, `scheduler`, and `worker` services all run with
  `restart: "no"` and stay up cleanly. The three placeholders idle
  (1-hour `time.sleep`) until their entrypoints are implemented; the API
  serves `/healthz` so the compose healthcheck passes.
- The stack uses `postgresql+psycopg` (v3) for `DATABASE_URL`. Adding
  `psycopg[binary]>=3.1` to `[project].dependencies` in `pyproject.toml`
  is the next step before the API can actually open a connection.
- `postgres_data` and `redis_data` are service-scoped named volumes;
  `app_data` is a shared named volume mounted at `/app/data` in every
  application service for runtime artefacts (logs, caches, downloads).

Validate the compose file at any time with:

```bash
docker compose config -q
```

## Pre-commit hooks

Install once with `uv run pre-commit install`. Hooks run **ruff** (`--fix` +
`ruff-format`) and **ty** on staged changes; CI runs the same checks
independently. The hook set is defined in `.pre-commit-config.yaml`.

## Repository bootstrap

```bash
# One-time: ensure the GitHub repo exists (manual or via gh)
gh repo create apply-pilot --private \
  --description "Telegram-first assistant for finding, scoring, reviewing, and applying to jobs."

# Then create labels, milestones, and issues idempotently
./scripts/bootstrap_github.sh
```

## Layout

```text
src/job_apply/
  db.py
  config.py
  features/
    orders/
      models.py
      repositories.py
      service.py
      schemas.py
tests/features/orders/
alembic/
scripts/
  bootstrap_github.sh
userstory/
  README.md            # detailed user story and architecture vision
```
