---
name: agentic-vsa-workflow
description: Autonomous parallel agentic workflow that drains GitHub issues using subagents, git worktrees (in .worktrees/), uv, Python 3.13, TDD, Vertical Slices Architecture, SQLAlchemy, Alembic, SemVer 2.0.0, code review, PR merge/issue closing, cleanup, and gh CLI.
disable-model-invocation: false
---

# Agentic VSA Workflow

## Мантра
* **Оркестрируй**: Главный агент не делает одну задачу и не останавливается; он параллельно ведёт backlog issues до исчерпания.
* **Изолируй**: Один агент — одна фича — один worktree в `.worktrees/`.
* **TDD**: Тест. Код. Рефакторинг.
* **DI в тестах**: Предпочитай dependency injection, fakes и in-memory реализации вместо `Mock`.
* **Скорость**: `uv` для зависимостей. Python 3.13.
* **Вертикаль**: Vertical Slices Architecture. Организуй код вокруг user/business capabilities, а не технических слоёв.
* **Версионируй**: SemVer 2.0.0. Версия сообщает совместимость публичного API.

## Vertical Slices Architecture

Организуй каждую фичу как независимый вертикальный срез: request/command, validation, бизнес-логика, persistence и тесты живут рядом и меняются вместе.

Предпочитай:
* `src/<package>/features/<feature>/...` для production-кода среза.
* `tests/features/<feature>/...` для тестов этого же среза.
* Локальные DTO/schema, handlers/services, repositories/gateways внутри feature-папки.
* DI через конструкторы/функции, чтобы тестировать срез через fakes/in-memory зависимости.
* Общий код только если он реально используется несколькими срезами и стабилен: `db`, `config`, shared primitives.

Избегай:
* Горизонтальных папок вида `services/`, `repositories/`, `models/`, если они собирают код разных бизнес-фич.
* Больших shared abstractions заранее.
* Cross-slice импортов без необходимости. Если один срезу нужны данные другого, используй явный публичный контракт или application-level orchestration.
* Тестов, которые проверяют технический слой вместо поведения конкретного среза.

Пример структуры:
```text
src/job_apply/features/orders/
  models.py
  repositories.py
  schemas.py
  service.py
tests/features/orders/
  test_order_service.py
```

## SemVer

Используй [Семантическое Версионирование 2.0.0](https://semver.org/lang/ru/) для пакета, публичного API, changelog и релизов.

Формат версии: `МАЖОРНАЯ.МИНОРНАЯ.ПАТЧ` (`X.Y.Z`).

* **PATCH** (`x.y.Z`): обратно совместимые исправления некорректного поведения.
* **MINOR** (`x.Y.z`): новая обратно совместимая функциональность публичного API; также bump minor при пометке публичного API как deprecated. При bump minor обнуляй patch.
* **MAJOR** (`X.y.z`): обратно несовместимые изменения публичного API. При bump major обнуляй minor и patch.
* **0.y.z**: начальная разработка. API нестабилен, но всё равно фиксируй смысл изменений.
* **1.0.0**: публичный API определён и считается стабильным.
* **Prerelease**: используй `-alpha`, `-beta`, `-rc.1` для нестабильных предварительных версий, например `1.2.0-rc.1`.
* **Build metadata**: используй `+...` только для метаданных сборки; они не влияют на приоритет версии, например `1.2.0+20260614`.

Перед релизом определи:
1. Что является публичным API: CLI, Python imports, HTTP API, DB schema для внешних потребителей, config/env vars, documented behavior.
2. Есть ли breaking changes.
3. Какой bump нужен: patch/minor/major.
4. Нужно ли обновить `pyproject.toml`, changelog, README, миграции и примеры.

## Автономный orchestration loop

Главный агент — оркестратор. Он не останавливается после открытия PR и не ждёт пользователя для обычного review/fix/merge цикла. Он сам запускает субагентов, контролирует статусы и берёт следующие задачи, пока backlog GitHub issues не закончится или пока не появится настоящий блокер: отсутствуют права, сломан внешний сервис, неоднозначное продуктовое решение, конфликт scope, требующий выбора пользователя.

### 1. Найди backlog и сформируй batch

Используй `gh` для чтения открытых issues. Фильтруй уже закрытые, уже связанные с активными PR, заблокированные и явно помеченные как `blocked`, `wontfix`, `needs-design`, `needs-decision`.

```bash
gh issue list --state open --limit 100
```

Сгруппируй issues по независимости:
* в один parallel batch бери только задачи с непересекающимися файлами, схемами БД, public API и бизнес-срезами;
* если есть риск конфликтов — уменьши batch или решай такие issues последовательно;
* каждая issue должна стать отдельным vertical slice и отдельным worktree;
* не бери две задачи, которые одновременно меняют одну миграционную цепочку, один public contract или один shared primitive, если их нельзя безопасно разделить.

Если issues закончились — заверши работу кратким отчётом: что было сделано, какие PR merged, какие issues закрыты, что осталось blocked.

### 2. Запусти implement-субагентов параллельно

На каждую issue из batch запусти отдельного субагента-исполнителя. Дай ему конкретный scope: номер issue, ветка, worktree, ожидаемые файлы/границы, запрет трогать чужие срезы.

Каждый implement-субагент делает полный feature delivery до PR:
```bash
git fetch
git worktree add .worktrees/issue-123-short-name -b issue-123-short-name
cd .worktrees/issue-123-short-name
uv sync
```

Внутри worktree:
* сначала пишет падающий тест на поведение vertical slice;
* реализует код по VSA;
* предпочитает DI/fakes/in-memory в тестах вместо `Mock`;
* если меняет модели внутри среза — создаёт Alembic migration;
* классифицирует SemVer impact: `patch`, `minor`, `major`, `none`;
* запускает targeted tests и релевантные проверки;
* коммитит, пушит branch, открывает PR с `Closes #123` / `Fixes #123` / `Resolves #123`;
* возвращает оркестратору: PR number/url, issue number, branch, worktree, summary, tests, SemVer impact, files changed, known risks.

PR body должен содержать:
* linked issue closing keyword;
* что изменилось;
* как проверено;
* SemVer impact;
* миграции, если есть;
* заметки по VSA boundaries.

### 3. Запусти review-субагента на каждый PR

После открытия PR не останавливайся. Для каждого PR запусти отдельного review-субагента с read-only задачей: проверить изменения и вернуть findings.

Review-субагент проверяет:
* correctness и edge cases;
* соответствие Vertical Slices Architecture;
* тесты на поведение среза, а не технические слои;
* DI/fakes/in-memory вместо лишних `Mock`;
* миграции и совместимость схемы;
* SemVer classification;
* security, performance, maintainability;
* что PR body содержит `Closes/Fixes/Resolves #issue`.

Review-субагент возвращает структурированный результат:
```text
status: approve | request-changes
blocking_findings:
  - file:line — problem — suggested fix
non_blocking_findings:
  - ...
```

### 4. Запусти fix-субагента, если review требует изменений

Если review status `request-changes`, запусти отдельного fix-субагента для того же PR/worktree/branch. Он должен внести исправления, добавить/обновить тесты, прогнать проверки, закоммитить и запушить в тот же PR.

```bash
gh pr checks <pr-number>
gh pr view <pr-number> --comments
uv run pytest tests/features/<feature>/ -v
git add .
git commit -m "fix: address review feedback"
git push
```

После фиксов снова запусти review-субагента. Повторяй loop:
```text
review → fixes → tests → push → review
```
пока review не `approve`, CI не зелёный и нет blocking comments.

### 5. Merge, закрытие issues и cleanup через субагента-завершителя

Когда PR approved и checks зелёные, запусти close-субагента для этого PR. Он выполняет merge, проверяет автоматическое закрытие issues и чистит всё лишнее.

Close-субагент:
* проверяет, что PR связан с issue через closing keyword;
* делает merge согласно правилам repo (`squash`, `rebase` или `merge`; по умолчанию `squash`);
* удаляет remote branch через GitHub, если это безопасно;
* проверяет, что связанные issues закрылись;
* если GitHub не закрыл issue автоматически — закрывает вручную с комментарием и ссылкой на PR;
* удаляет локальный worktree и локальную branch;
* выполняет prune;
* удаляет временные runtime/cache artifacts, созданные этой задачей.

```bash
gh pr merge <pr-number> --squash --delete-branch
gh issue view 123
# если issue не закрылась автоматически:
gh issue close 123 --comment "Closed by PR #<pr-number>."
cd ../../
git worktree remove .worktrees/issue-123-short-name
git branch -d issue-123-short-name
git fetch --prune
```

Close-субагент возвращает: merged PR, closed issues, removed worktree/branch, cleanup summary, leftovers if any.

### 6. Продолжай до исчерпания issues

После завершения batch главный агент сразу перечитывает backlog:
```bash
gh issue list --state open --limit 100
```

Затем формирует следующий независимый parallel batch и повторяет implement → review → fixes → merge/close → cleanup. Не спрашивай пользователя “что дальше?”, если есть открытые неблокированные issues и есть безопасный независимый scope.

Останавливайся только когда:
* нет открытых неблокированных issues;
* оставшиеся issues требуют продуктового решения, секретов, внешних доступов или human approval;
* merge запрещён branch protection/rules и у агента нет прав;
* обнаружен конфликт архитектуры/scope, который нельзя безопасно решить автономно.

Финальный отчёт должен содержать:
* сколько issues обработано;
* список merged PR и закрытых issues;
* какие проверки запускались;
* какие worktrees/branches очищены;
* что осталось blocked и почему.

## Single-slice worker rules

Эти правила применяются каждым implement/fix-субагентом внутри своего worktree.

### Изоляция
Создай ветку в локальной папке `.worktrees/`. Убедись, что `.worktrees/` есть в `.gitignore`.

Задачи оформляй как GitHub issues через `gh issue create` или комментарии к существующим issue. Локальные `.md` файлы с планами в корне репо не создавай — они шумят в рабочем дереве, рассинхронизируются с реальным backlog `gh issue list`, и блокируют cleanup. Если план временный — держи его в issue body и закрывай issue когда задача решена.

### TDD (Pytest)
Сначала падающий тест на поведение вертикального среза. Затем код фичи.

Тестируй feature-slice целиком на уровне use case/handler/service, а не каждый технический слой отдельно. Проверяй observable behavior: результат, состояние, событие, запись в БД, ошибку.

В тестах предпочитай DI:
* Передавай зависимости через конструкторы/функции внутри среза.
* Используй fakes, stubs и in-memory реализации для репозиториев, клиентов и gateway.
* Используй реальные value objects/DTO и минимальный in-memory state вместо настройки цепочек `Mock`.
* Используй `Mock` только на внешних границах (HTTP, broker, filesystem, clock) или когда нужно проверить конкретное взаимодействие, а не состояние/результат.

```bash
uv run pytest tests/features/<feature>/ -v
```

### Состояние (Alembic)
Изменил модели внутри среза — создай миграцию. Миграция является частью delivery этого vertical slice.
```bash
uv run alembic revision --autogenerate -m "feat_name"
uv run alembic upgrade head
```

### SemVer-проверка
Перед PR классифицируй изменение по SemVer.
```text
patch: bug fix без изменения публичного API
minor: новая обратно совместимая возможность или deprecation
major: breaking change публичного API
none: internal-only change без релизного эффекта
```

Если задача меняет публикуемый пакет или внешний контракт, обнови версию в одном источнике истины проекта (`pyproject.toml`, package `__version__` или другой принятый механизм) и синхронизируй changelog/README.

### PR
PR должен закрывать issue после merge через `Closes #N`, `Fixes #N` или `Resolves #N`. Не считай задачу завершённой на этапе PR opened: задача завершена только после review approval, merge, закрытия связанных issues и cleanup.