---
name: agentic-vsa-workflow
description: Parallel agentic workflow using git worktree (in .worktrees/), uv, Python 3.13, TDD, SQLAlchemy, Alembic, and gh CLI.
disable-model-invocation: false
---

# Agentic VSA Workflow

## Мантра
* **Изолируй**: Один агент — одна фича — один worktree в `.worktrees/`.
* **TDD**: Тест. Код. Рефакторинг.
* **Скорость**: `uv` для зависимостей. Python 3.13.
* **Вертикаль**: VSA. Никаких слоев.

## Цикл

### 1. Изоляция (Worktree + uv)
Создай ветку в локальной папке `.worktrees/`. *(Убедись, что `.worktrees/` есть в `.gitignore`)*.
```bash
git fetch
git worktree add .worktrees/feature-name -b feature-name
cd .worktrees/feature-name
uv sync
```

### 2. TDD (Pytest)
Сначала падающий тест. Затем код фичи.
```bash
uv run pytest tests/features/orders/ -v
```

### 3. Состояние (Alembic)
Изменил модели — создай миграцию.
```bash
uv run alembic revision --autogenerate -m "feat_name"
uv run alembic upgrade head
```

### 4. Доставка (GH CLI)
Коммит, пуш, пулл-реквест.
```bash
git add .
git commit -m "feat: description"
git push -u origin feature-name
gh pr create --fill
```

### 5. Очистка
Удали worktree, вернись в корень.
```bash
cd ../../
git worktree remove .worktrees/feature-name
git branch -d feature-name
```