# План запуска GitHub-проекта

## Рекомендованное имя

**ApplyPilot**

Почему подходит:
- короткое и запоминаемое;
- отражает идею «автопилота» для откликов на вакансии;
- не привязано только к hh, оставляет место для Telegram, карьерных сайтов и других источников;
- хорошо смотрится как имя репозитория: `apply-pilot`.

Альтернативы:
- `VacancyPilot` — более явно про вакансии, но менее продуктово;
- `CareerCopilot` — понятно, но звучит шире и может конфликтовать с Copilot-ассоциациями;
- `ApplyFlow` — хорошо про workflow, но менее уникально;
- `JobScout AI` — хорошо про поиск, но хуже покрывает apply workflow;
- `OfferRadar` — хорошо про поиск, но не про отклики.

## Целевой GitHub repo

- Owner: текущий GitHub-пользователь из `gh auth status`.
- Repo name: `apply-pilot`.
- Visibility: лучше `private`, пока есть интеграции с hh, Telegram, LLM и секретами.
- Description: `Telegram-first assistant for finding, scoring, reviewing, and applying to jobs.`
- Homepage: пусто до появления dashboard.
- Topics: `fastapi`, `python`, `telegram-bot`, `job-search`, `hh`, `llm`, `postgresql`, `redis`.

Команда после выхода из plan mode:

```bash
gh repo create apply-pilot --private --description "Telegram-first assistant for finding, scoring, reviewing, and applying to jobs." --source . --remote origin --push
```

Если remote уже есть:

```bash
gh repo create apply-pilot --private --description "Telegram-first assistant for finding, scoring, reviewing, and applying to jobs."
git remote add origin git@github.com:<owner>/apply-pilot.git
git push -u origin main
```

## Labels

Создать базовый набор labels:

- `type:epic`
- `type:story`
- `type:task`
- `type:bug`
- `area:foundation`
- `area:auth`
- `area:telegram`
- `area:resume`
- `area:hh`
- `area:sources`
- `area:scoring`
- `area:cover-letter`
- `area:apply-worker`
- `area:web-api`
- `area:admin`
- `area:observability`
- `priority:p0`
- `priority:p1`
- `priority:p2`
- `mvp`
- `post-mvp`

## Milestones

### M0 — Foundation

Goal: репозиторий и технический каркас готовы для разработки vertical slices.

Issues:
1. `M0: Initialize repository and project metadata`
2. `M0: Configure uv, Python 3.13, ruff, mypy, pytest`
3. `M0: Add FastAPI app factory and health endpoint`
4. `M0: Add PostgreSQL, SQLAlchemy, Alembic baseline`
5. `M0: Add Redis and background runtime skeleton`
6. `M0: Add Dockerfile and docker-compose for local stack`
7. `M0: Add CI for lint, typing, tests, migrations`
8. `M0: Define vertical slice conventions and shared utilities`

### M1 — Users, Resumes, Telegram

Goal: пользователь может зарегистрироваться, подключить Telegram, загрузить резюме и настроить search profile.

Issues:
1. `M1: Implement user registration and login`
2. `M1: Implement user/session persistence`
3. `M1: Implement Telegram account linking flow`
4. `M1: Add Telegram bot command skeleton`
5. `M1: Implement resume upload API`
6. `M1: Extract and store resume text`
7. `M1: Implement search profile CRUD`
8. `M1: Add audit logs for account setup events`

### M2 — hh Collector

Goal: система получает вакансии из hh, нормализует их и создаёт match-кандидаты.

Issues:
1. `M2: Implement hh account connection`
2. `M2: Store hh credentials securely`
3. `M2: Sync resumes metadata from hh`
4. `M2: Implement hh vacancy search adapter`
5. `M2: Normalize vacancy data into canonical model`
6. `M2: Deduplicate vacancies by source identity`
7. `M2: Create vacancy matches for search profiles`
8. `M2: Capture screening questions when available`

### M3 — Scoring and Drafts

Goal: вакансии фильтруются, оцениваются LLM и получают черновик сопроводительного письма.

Issues:
1. `M3: Implement quick filter rules`
2. `M3: Persist quick filter decisions and reasons`
3. `M3: Implement deep LLM scoring pipeline`
4. `M3: Store score, explanation, and prompt version`
5. `M3: Generate first cover letter draft`
6. `M3: Regenerate cover letter draft on request`
7. `M3: Support user cover letter style preferences`
8. `M3: Prepare basic screening question answers`

### M4 — Telegram Review Loop

Goal: пользователь ежедневно ревьюит вакансии в Telegram и принимает решения.

Issues:
1. `M4: Send daily Telegram statistics digest`
2. `M4: Render vacancy review card in Telegram`
3. `M4: Implement accept action`
4. `M4: Implement reject action`
5. `M4: Implement defer action`
6. `M4: Implement regenerate draft action`
7. `M4: Create apply job after accept`
8. `M4: Add Telegram workflow integration tests`

### M5 — Apply Worker MVP

Goal: accepted vacancies are sent to hh safely with limits, retries, and status history.

Issues:
1. `M5: Implement apply job queue model`
2. `M5: Implement apply worker runtime`
3. `M5: Implement hh apply adapter`
4. `M5: Add anti-spam limits and rate limiting`
5. `M5: Add retry policy for retryable failures`
6. `M5: Add idempotency key for apply submission`
7. `M5: Persist apply status history`
8. `M5: Notify user about apply result`

### M6 — Web/API Dashboard

Goal: минимальный кабинет для просмотра статуса, вакансий, профилей и health/admin-информации.

Issues:
1. `M6: Add dashboard API summary endpoint`
2. `M6: Add vacancy list API with filters`
3. `M6: Add search profile settings API`
4. `M6: Add apply history API`
5. `M6: Add minimal frontend shell`
6. `M6: Add basic admin health page`
7. `M6: Add admin worker and integration status view`

### M7 — Additional Sources

Goal: добавить источники вакансий вне hh через единый source adapter contract.

Issues:
1. `M7: Define source adapter interface`
2. `M7: Implement Telegram channel source adapter`
3. `M7: Implement company careers page source adapter`
4. `M7: Evaluate Habr Career adapter`
5. `M7: Add source-specific failure isolation`
6. `M7: Add source observability metrics`

### M8 — Intelligence Improvements

Goal: улучшить качество scoring, писем и персонализации на основе поведения пользователя.

Issues:
1. `M8: Add learning signals from user rejections`
2. `M8: Add prompt versioning registry`
3. `M8: Add A/B testing for scoring prompts`
4. `M8: Add personal writing style memory`
5. `M8: Add analytics by source and search profile`
6. `M8: Add scoring quality review dashboard`

## MVP Roadmap

MVP состоит из milestones `M0`–`M5`.

Рекомендуемый порядок:
1. `M0 — Foundation`
2. `M1 — Users, Resumes, Telegram`
3. `M2 — hh Collector`
4. `M3 — Scoring and Drafts`
5. `M4 — Telegram Review Loop`
6. `M5 — Apply Worker MVP`

Post-MVP:
1. `M6 — Web/API Dashboard`
2. `M7 — Additional Sources`
3. `M8 — Intelligence Improvements`

## GitHub Project Roadmap View

Создать GitHub Projects v2 с названием `ApplyPilot Roadmap`.

Поля:
- `Status`: Backlog, Ready, In progress, In review, Done
- `Milestone`: GitHub milestone
- `Area`: labels `area:*`
- `Priority`: labels `priority:*`
- `MVP`: label `mvp` / `post-mvp`

Views:
- `Roadmap`: grouped by milestone
- `MVP Board`: filtered by `label:mvp`
- `Current Milestone`: filtered by active milestone
- `Areas`: grouped by `area:*`

## Автоматизация создания через gh

После выхода из plan mode выполнить:

```bash
gh auth status
gh repo create apply-pilot --private --description "Telegram-first assistant for finding, scoring, reviewing, and applying to jobs." --source . --remote origin --push
```

Создание milestones через REST API:

```bash
gh api repos/<owner>/apply-pilot/milestones -f title='M0 — Foundation' -f description='Repository and technical foundation for vertical slice development.'
gh api repos/<owner>/apply-pilot/milestones -f title='M1 — Users, Resumes, Telegram' -f description='Registration, Telegram linking, resume upload, search profile setup.'
gh api repos/<owner>/apply-pilot/milestones -f title='M2 — hh Collector' -f description='hh account connection, vacancy collection, normalization, matching.'
gh api repos/<owner>/apply-pilot/milestones -f title='M3 — Scoring and Drafts' -f description='Quick filtering, LLM scoring, cover letters, screening answers.'
gh api repos/<owner>/apply-pilot/milestones -f title='M4 — Telegram Review Loop' -f description='Daily digest and Telegram accept/reject/regenerate/defer workflow.'
gh api repos/<owner>/apply-pilot/milestones -f title='M5 — Apply Worker MVP' -f description='Safe hh apply worker with queue, limits, retries, and status history.'
gh api repos/<owner>/apply-pilot/milestones -f title='M6 — Web/API Dashboard' -f description='Minimal dashboard and admin health views.'
gh api repos/<owner>/apply-pilot/milestones -f title='M7 — Additional Sources' -f description='Adapters for Telegram channels, company sites, and other job boards.'
gh api repos/<owner>/apply-pilot/milestones -f title='M8 — Intelligence Improvements' -f description='Learning signals, prompt versioning, personalization, analytics.'
```

Issues лучше создать скриптом через `gh issue create`, чтобы назначать milestone и labels последовательно. Для всех `M0`–`M5` добавить label `mvp`, для `M6`–`M8` — `post-mvp`.

## Проверка после выполнения

1. `gh repo view <owner>/apply-pilot --web` открывает новый репозиторий.
2. `gh issue list --repo <owner>/apply-pilot --limit 100` показывает созданные issues.
3. `gh api repos/<owner>/apply-pilot/milestones` показывает 9 milestones.
4. В GitHub Project есть views `Roadmap`, `MVP Board`, `Current Milestone`, `Areas`.
5. README проекта обновлён с новым названием `ApplyPilot` и ссылкой на roadmap.
