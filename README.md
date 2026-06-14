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

Issue tracking, milestones, and labels are created via `scripts/bootstrap_github.sh`
after the repository exists on GitHub.

## Quick start (local)

```bash
uv sync --extra dev
uv run pytest -v
```

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
