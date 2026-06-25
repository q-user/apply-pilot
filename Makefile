# One-shot convenience targets for worktree maintenance.
# Codifies the procedure we ran by hand after shipping the SSL/MinCifry fix.

.PHONY: help cleanup-worktrees cleanup-worktrees-dry-run

help:                           ## show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-30s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

cleanup-worktrees:              ## remove .worktrees/* whose branch is merged into main (prompts via --dry-run first if stdout is tty)
	@bash scripts/worktree-cleanup.sh

cleanup-worktrees-dry-run:      ## show what \`make cleanup-worktrees\` would do, without removing anything
	@bash scripts/worktree-cleanup.sh --dry-run
