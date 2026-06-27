# One-shot convenience targets for worktree maintenance.
# Codifies the procedure we ran by hand after shipping the SSL/MinCifry fix.

.PHONY: help cleanup-worktrees cleanup-worktrees-dry-run ai-archive

help:                           ## show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-30s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

cleanup-worktrees:              ## remove .worktrees/* whose branch is merged into main (prompts via --dry-run first if stdout is tty)
	@bash scripts/worktree-cleanup.sh

cleanup-worktrees-dry-run:      ## show what \`make cleanup-worktrees\` would do, without removing anything
	@bash scripts/worktree-cleanup.sh --dry-run

# Bundle the codebase for AI analysis. Excludes VCS metadata, virtualenvs,
# tooling caches, local runtime data, worktrees and agent state so the
# resulting archive contains only code + config + docs the model should see.
ANALYSIS_ARCHIVE ?= /tmp/analysis.zip

ai-archive:                    ## build a zip of the codebase for AI analysis at $(ANALYSIS_ARCHIVE)
	zip -r $(ANALYSIS_ARCHIVE) \
		src tests alembic.ini \
		Dockerfile docker-compose.yml pyproject.toml \
		.python-version .pre-commit-config.yaml .dockerignore .gitignore \
		-x \
		"**/__pycache__/*" \
		"**/*.pyc" "**/*.pyo" "**/*.pyd" \
		"**/*.egg-info/*" \
		"**/.pytest_cache/*" \
		"**/.ruff_cache/*" \
		"**/.ty_cache/*" \
		"**/.mypy_cache/*" \
		"**/.coverage" \
		"**/htmlcov/*" \
		"**/.venv/*" \
		"**/.worktrees/*" \
		"**/.data/*" \
		"**/.agents/*" \
		"**/.opencode/*" \
		"**/.idea/*" \
		"**/.github/*" \
		"alembic/versions/*" "alembic/versions/**" \
		"**/_plans/*" \
		"**/userstory/*" \
		"**/docs/*" \
		"tests/features/*" "tests/features/**" \
		"**/*.db" "**/*.sqlite3" "**/*.log" \
		"**/*.so" \
		"**/build/*" "**/dist/*" "**/.eggs/*" \
		"**/.dev.db" \
		"**/dev.db" \
		"*.txt" "*.md" "*.rst"
	@echo "wrote $(ANALYSIS_ARCHIVE)"
	@ls -lh $(ANALYSIS_ARCHIVE) | awk '{printf "size: %s (%s bytes)\n", $$5, $$5}'
