#!/usr/bin/env bash
# Bootstrap GitHub project: create labels, milestones, and issues.
# Requires: gh auth login completed and the target repo to already exist.
# Usage: scripts/bootstrap_github.sh [owner]
#   owner — optional GitHub owner (defaults to the currently authenticated user).
set -uo pipefail

OWNER="${1:-$(gh api user --jq .login 2>/dev/null || true)}"
REPO="apply-pilot"

if [[ -z "${OWNER}" ]]; then
  echo "ERROR: cannot determine GitHub owner. Pass it as the first argument or run 'gh auth login'." >&2
  exit 1
fi

if ! gh api -H "Accept: application/vnd.github+json" "repos/${OWNER}/${REPO}" >/dev/null 2>&1; then
  echo "ERROR: repository ${OWNER}/${REPO} does not exist. Create it first (gh repo create ${REPO} --public ...)." >&2
  exit 1
fi

echo "== Bootstrap target: ${OWNER}/${REPO} =="

# 1. Labels — pass as JSON via gh label create --force, color must be 6-char hex without '#'
read -r -d '' LABELS_JSON <<'JSON' || true
[
  {"name":"type:epic","color":"d73b4b","description":"Grouping issue for an epic"},
  {"name":"type:story","color":"0e8a16","description":"User-facing story"},
  {"name":"type:task","color":"5319e7","description":"Engineering task"},
  {"name":"type:bug","color":"d93f0b","description":"Defect to fix"},
  {"name":"area:foundation","color":"1d76db","description":"Project foundation"},
  {"name":"area:auth","color":"bfd4f2","description":"Authentication"},
  {"name":"area:telegram","color":"006b75","description":"Telegram bot and integration"},
  {"name":"area:resume","color":"bfdadc","description":"Resume storage and extraction"},
  {"name":"area:hh","color":"fbca04","description":"hh.ru integration"},
  {"name":"area:sources","color":"c2e0c6","description":"Vacancy source adapters"},
  {"name":"area:scoring","color":"7057ff","description":"Scoring pipeline"},
  {"name":"area:cover-letter","color":"fef2c0","description":"Cover letter generation"},
  {"name":"area:apply-worker","color":"b60205","description":"Apply worker and queue"},
  {"name":"area:web-api","color":"0e8a16","description":"Web/API surface"},
  {"name":"area:admin","color":"5319e7","description":"Admin tools"},
  {"name":"area:observability","color":"cccccc","description":"Logs, metrics, audits"},
  {"name":"priority:p0","color":"b60205","description":"Top priority"},
  {"name":"priority:p1","color":"d93f0b","description":"High priority"},
  {"name":"priority:p2","color":"fbca04","description":"Medium priority"},
  {"name":"mvp","color":"0e8a16","description":"In-scope for MVP"},
  {"name":"post-mvp","color":"cccccc","description":"Post-MVP scope"}
]
JSON

echo "$LABELS_JSON" | jq -c '.[]' | while read -r entry; do
  name=$(echo "$entry" | jq -r .name)
  color=$(echo "$entry" | jq -r .color)
  desc=$(echo "$entry" | jq -r .description)
  if gh label create "$name" --color "$color" --description "$desc" --repo "${OWNER}/${REPO}" --force >/dev/null 2>&1; then
    :
  else
    echo "  ! failed to create label $name"
  fi
done
echo "-- Labels ensured"

# 2. Milestones
declare -A MILESTONES=(
  ["M0"]="M0 — Foundation|Repository and technical foundation for vertical slice development."
  ["M1"]="M1 — Users, Resumes, Telegram|Registration, Telegram linking, resume upload, search profile setup."
  ["M2"]="M2 — hh Collector|hh account connection, vacancy collection, normalization, matching."
  ["M3"]="M3 — Scoring and Drafts|Quick filtering, LLM scoring, cover letters, screening answers."
  ["M4"]="M4 — Telegram Review Loop|Daily digest and Telegram accept/reject/regenerate/defer workflow."
  ["M5"]="M5 — Apply Worker MVP|Safe hh apply worker with queue, limits, retries, and status history."
  ["M6"]="M6 — Web/API Dashboard|Minimal dashboard and admin health views."
  ["M7"]="M7 — Additional Sources|Adapters for Telegram channels, company sites, and other job boards."
  ["M8"]="M8 — Intelligence Improvements|Learning signals, prompt versioning, personalization, analytics."
)

declare -A MILESTONE_TITLES
declare -A MILESTONE_IDS
for key in M0 M1 M2 M3 M4 M5 M6 M7 M8; do
  IFS='|' read -r title desc <<< "${MILESTONES[$key]}"
  existing=$(gh api "repos/${OWNER}/${REPO}/milestones?state=all&per_page=100" --jq ".[] | select(.title==\"${title}\") | .number" 2>/dev/null | head -n1 || true)
  if [[ -z "${existing:-}" ]]; then
    number=$(gh api "repos/${OWNER}/${REPO}/milestones" -X POST -f "title=${title}" -f "description=${desc}" --jq .number 2>/dev/null || echo "")
    if [[ -n "${number}" ]]; then
      echo "-- Created milestone ${title} (#${number})"
    else
      echo "  ! failed to create milestone ${title}"
      continue
    fi
  else
    number="${existing}"
    echo "-- Reusing milestone ${title} (#${number})"
  fi
  MILESTONE_IDS[$key]="${number}"
  MILESTONE_TITLES[$key]="${title}"
done

# Helper: list existing issue titles (with retry on transient TLS errors)
existing_titles() {
  for attempt in 1 2 3 4; do
    if gh issue list --repo "${OWNER}/${REPO}" --state all --limit 200 --json title --jq '.[].title' 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# 3. Issues
create_issue() {
  local title="$1"; local labels_csv="$2"; local body="$3"; local milestone_key="$4"
  local milestone="${MILESTONE_TITLES[$milestone_key]:-}"
  if [[ -z "${milestone}" ]]; then
    echo "  ! missing milestone for ${title}"
    return 1
  fi
  if existing_titles | grep -Fxq "${title}"; then
    echo "  · skip (exists) ${title}"
    return 0
  fi
  local args=(--repo "${OWNER}/${REPO}" --title "${title}" --body "${body}" --milestone "${milestone}")
  if [[ -n "${labels_csv}" ]]; then
    IFS=',' read -ra LBL_ARR <<< "${labels_csv}"
    for lbl in "${LBL_ARR[@]}"; do
      args+=(--label "${lbl}")
    done
  fi
  if gh issue create "${args[@]}" >/dev/null 2>&1; then
    echo "  · created ${title}"
  else
    err=$(gh issue create "${args[@]}" 2>&1 >/dev/null | tail -3)
    echo "  ! failed to create ${title}: ${err}"
  fi
}

# M0
create_issue "M0: Initialize repository and project metadata" "type:task,area:foundation,priority:p0" "Add repository name, description, topics, and LICENSE." M0
create_issue "M0: Configure uv, Python 3.13, ruff, mypy, pytest" "type:task,area:foundation,priority:p0" "Pin Python 3.13 in pyproject.toml, configure ruff/mypy/pytest, and add uv scripts." M0
create_issue "M0: Add FastAPI app factory and health endpoint" "type:story,area:foundation,priority:p0" "Implement FastAPI app factory pattern and /healthz endpoint with structured logging." M0
create_issue "M0: Add PostgreSQL, SQLAlchemy, Alembic baseline" "type:story,area:foundation,priority:p0" "Wire SQLAlchemy 2.x engine, Alembic baseline migration, and session scope." M0
create_issue "M0: Add Redis and background runtime skeleton" "type:story,area:foundation,priority:p0" "Wire Redis client and a small runtime helper for scheduler/worker processes." M0
create_issue "M0: Add Dockerfile and docker-compose for local stack" "type:task,area:foundation,priority:p0" "Provide Dockerfile and docker-compose with API, bot, scheduler, worker, postgres, redis." M0
create_issue "M0: Add CI for lint, typing, tests, migrations" "type:task,area:foundation,priority:p0" "GitHub Actions workflow: ruff, mypy, pytest, alembic upgrade against disposable DB." M0
create_issue "M0: Define vertical slice conventions and shared utilities" "type:task,area:foundation,priority:p0" "Document VSA conventions and add shared schemas/errors/logging helpers." M0

# M1
create_issue "M1: Implement user registration and login" "type:story,area:auth,priority:p0,mvp" "Email/password registration and login endpoints, password hashing, validation." M1
create_issue "M1: Implement user/session persistence" "type:story,area:auth,priority:p0,mvp" "Session/token model, refresh handling, logout, and audit log entries." M1
create_issue "M1: Implement Telegram account linking flow" "type:story,area:telegram,priority:p0,mvp" "Generate one-time token, validate via Telegram bot, store TelegramAccount row." M1
create_issue "M1: Add Telegram bot command skeleton" "type:story,area:telegram,priority:p0,mvp" "Aiogram/PTB bot process with /start, /help, and dispatcher wiring." M1
create_issue "M1: Implement resume upload API" "type:story,area:resume,priority:p0,mvp" "Multipart upload, file type validation, storage abstraction, persisted metadata." M1
create_issue "M1: Extract and store resume text" "type:story,area:resume,priority:p0,mvp" "PDF/DOCX text extraction, store plain text and structured fields in Resume." M1
create_issue "M1: Implement search profile CRUD" "type:story,area:resume,priority:p0,mvp" "CRUD for SearchProfile: title, keywords, salary, location, schedule, etc." M1
create_issue "M1: Add audit logs for account setup events" "type:task,area:observability,priority:p1,mvp" "AuditLog entries for register, login, telegram_link, resume_upload, profile_update." M1

# M2
create_issue "M2: Implement hh account connection" "type:story,area:hh,priority:p0,mvp" "OAuth or token-based hh auth flow, redirect/callback handlers." M2
create_issue "M2: Store hh credentials securely" "type:task,area:hh,priority:p0,mvp" "Encrypted at rest, redact from logs, support rotation." M2
create_issue "M2: Sync resumes metadata from hh" "type:story,area:hh,priority:p1,mvp" "Fetch user's hh resumes, link them to internal Resume by hh_resume_id." M2
create_issue "M2: Implement hh vacancy search adapter" "type:story,area:hh,priority:p0,mvp" "Adapter implementing SourceAdapter contract over hh search API." M2
create_issue "M2: Normalize vacancy data into canonical model" "type:story,area:sources,priority:p0,mvp" "Map hh fields to internal Vacancy model, including salary normalization." M2
create_issue "M2: Deduplicate vacancies by source identity" "type:story,area:sources,priority:p0,mvp" "Use (source, source_vacancy_id) unique key plus content hash fallback." M2
create_issue "M2: Create vacancy matches for search profiles" "type:story,area:sources,priority:p0,mvp" "Per active SearchProfile, generate VacancyMatch rows with initial status." M2
create_issue "M2: Capture screening questions when available" "type:story,area:hh,priority:p1,mvp" "Persist questions via ScreeningQuestion entities attached to VacancyMatch." M2

# M3
create_issue "M3: Implement quick filter rules" "type:story,area:scoring,priority:p0,mvp" "Cheap rule-based filter (keywords, salary, location, schedule) with reasons." M3
create_issue "M3: Persist quick filter decisions and reasons" "type:task,area:scoring,priority:p0,mvp" "Store decision, rule hits, and reasons in VacancyMatch." M3
create_issue "M3: Implement deep LLM scoring pipeline" "type:story,area:scoring,priority:p0,mvp" "Prompt template + LLM call, parse score 0-100, store explanation and prompt_version." M3
create_issue "M3: Store score, explanation, and prompt version" "type:task,area:scoring,priority:p0,mvp" "Add fields to VacancyMatch and keep prompt_version in registry table." M3
create_issue "M3: Generate first cover letter draft" "type:story,area:cover-letter,priority:p0,mvp" "Per VacancyMatch create CoverLetterDraft using resume + vacancy context." M3
create_issue "M3: Regenerate cover letter draft on request" "type:story,area:cover-letter,priority:p0,mvp" "Keep history of drafts and regenerate via Telegram action." M3
create_issue "M3: Support user cover letter style preferences" "type:story,area:cover-letter,priority:p1,mvp" "Store user style preferences, feed them into cover letter prompt." M3
create_issue "M3: Prepare basic screening question answers" "type:story,area:cover-letter,priority:p1,mvp" "LLM suggests answers; persist ScreeningQuestionAnswer rows." M3

# M4
create_issue "M4: Send daily Telegram statistics digest" "type:story,area:telegram,priority:p0,mvp" "Scheduler sends daily digest with new/seen/scored/accepted/applied counts." M4
create_issue "M4: Render vacancy review card in Telegram" "type:story,area:telegram,priority:p0,mvp" "Inline keyboard: Accept / Reject / Defer / Regenerate; show score and summary." M4
create_issue "M4: Implement accept action" "type:story,area:telegram,priority:p0,mvp" "Set match status to accepted and create ApplyJob in queued state." M4
create_issue "M4: Implement reject action" "type:story,area:telegram,priority:p0,mvp" "Mark VacancyMatch as rejected, capture reason signal for learning." M4
create_issue "M4: Implement defer action" "type:story,area:telegram,priority:p0,mvp" "Move match to deferred, surface in next digest." M4
create_issue "M4: Implement regenerate draft action" "type:story,area:telegram,priority:p0,mvp" "Trigger M3.6 and update message in Telegram." M4
create_issue "M4: Create apply job after accept" "type:task,area:apply-worker,priority:p0,mvp" "Enqueue ApplyJob with idempotency key derived from (user, vacancy)." M4
create_issue "M4: Add Telegram workflow integration tests" "type:task,area:observability,priority:p1,mvp" "End-to-end tests for digest and review actions using a fake bot transport." M4

# M5
create_issue "M5: Implement apply job queue model" "type:story,area:apply-worker,priority:p0,mvp" "ApplyJob table: status, attempts, last_error, scheduled_at, idempotency_key." M5
create_issue "M5: Implement apply worker runtime" "type:story,area:apply-worker,priority:p0,mvp" "Worker process polls/leases jobs, emits structured logs, handles graceful shutdown." M5
create_issue "M5: Implement hh apply adapter" "type:story,area:apply-worker,priority:p0,mvp" "Submit application through hh API with attached cover letter and answers." M5
create_issue "M5: Add anti-spam limits and rate limiting" "type:task,area:apply-worker,priority:p0,mvp" "Daily/hourly caps per user, jittered delay between submissions, Redis lock." M5
create_issue "M5: Add retry policy for retryable failures" "type:task,area:apply-worker,priority:p0,mvp" "Exponential backoff with cap, distinct handling for non-retryable hh errors." M5
create_issue "M5: Add idempotency key for apply submission" "type:task,area:apply-worker,priority:p0,mvp" "Pass idempotency_key to hh, persist hh response, dedupe retries." M5
create_issue "M5: Persist apply status history" "type:task,area:apply-worker,priority:p0,mvp" "Append-only ApplyStatusEvent entries for every transition." M5
create_issue "M5: Notify user about apply result" "type:story,area:telegram,priority:p1,mvp" "Send Telegram notification with outcome and link to history." M5

# M6
create_issue "M6: Add dashboard API summary endpoint" "type:story,area:web-api,priority:p1,post-mvp" "Aggregate counts per status, last 7d activity, top sources." M6
create_issue "M6: Add vacancy list API with filters" "type:story,area:web-api,priority:p1,post-mvp" "Pagination, filters by status/source/score, sort by score or date." M6
create_issue "M6: Add search profile settings API" "type:story,area:web-api,priority:p1,post-mvp" "CRUD endpoints for SearchProfile from the dashboard." M6
create_issue "M6: Add apply history API" "type:story,area:web-api,priority:p1,post-mvp" "Apply jobs with status history and current attempt info." M6
create_issue "M6: Add minimal frontend shell" "type:task,area:web-api,priority:p2,post-mvp" "Server-rendered or SPA shell consuming the dashboard API." M6
create_issue "M6: Add basic admin health page" "type:story,area:admin,priority:p2,post-mvp" "Show service health, last runs of workers, Redis/DB status." M6
create_issue "M6: Add admin worker and integration status view" "type:story,area:admin,priority:p2,post-mvp" "Queue depth, failed jobs, hh/Telegram integration health, retry counts." M6

# M7
create_issue "M7: Define source adapter interface" "type:story,area:sources,priority:p1,post-mvp" "Stable SourceAdapter Protocol: search, fetch_details, normalize, healthcheck." M7
create_issue "M7: Implement Telegram channel source adapter" "type:story,area:sources,priority:p2,post-mvp" "Listen to configured channels, parse vacancy posts, normalize." M7
create_issue "M7: Implement company careers page source adapter" "type:story,area:sources,priority:p2,post-mvp" "Per-site parser configuration, RSS/HTML scraping with retries." M7
create_issue "M7: Evaluate Habr Career adapter" "type:task,area:sources,priority:p2,post-mvp" "Spike: API/HTML availability, terms of use, value assessment." M7
create_issue "M7: Add source-specific failure isolation" "type:task,area:observability,priority:p1,post-mvp" "Per-source circuit breaker and degraded mode without blocking other sources." M7
create_issue "M7: Add source observability metrics" "type:task,area:observability,priority:p2,post-mvp" "Per-source counts: fetched, normalized, deduped, failed, duration." M7

# M8
create_issue "M8: Add learning signals from user rejections" "type:story,area:scoring,priority:p2,post-mvp" "Capture rejection reasons, feed into feedback store for prompt tuning." M8
create_issue "M8: Add prompt versioning registry" "type:task,area:scoring,priority:p2,post-mvp" "DB-backed registry of prompts with version, owner, status." M8
create_issue "M8: Add A/B testing for scoring prompts" "type:story,area:scoring,priority:p2,post-mvp" "Bucket users/jobs, log outcomes, compare scoring quality." M8
create_issue "M8: Add personal writing style memory" "type:story,area:cover-letter,priority:p2,post-mvp" "Per-user style summary updated from accepted letters." M8
create_issue "M8: Add analytics by source and search profile" "type:story,area:web-api,priority:p2,post-mvp" "Dashboards: funnel by source, conversion by profile, time-to-apply." M8
create_issue "M8: Add scoring quality review dashboard" "type:story,area:admin,priority:p2,post-mvp" "Manual review queue for low-confidence matches." M8

echo "== Done =="
