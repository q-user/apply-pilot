#!/usr/bin/env bash
# scripts/worktree-cleanup.sh
#
# Codify the worktree cleanup procedure.
#
# For each entry in `git worktree list --porcelain`:
#   - skip the repo-root checkout (= main worktree)
#   - skip prunable entries (git flags them with "prunable")
#   - skip bare / detached worktrees (nothing safe to remove)
#   - refuse any worktree whose branch is ahead of main
#   - refuse dirty worktrees unless --force is given
#   - dry-run / remove + `git branch -d` for merged-into-main branches
# After the walk, run `git remote prune origin` to drop stale tracking refs.
#
# Defaults to dry-run=false (real cleanup). Pass --dry-run to preview only.
# Pass --force to override the dirty-refuse guard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_PREFIX="[worktree-cleanup]"

DRY_RUN=0
FORCE=0

usage() {
    cat <<USAGE
Usage: $(basename "$0") [--dry-run] [--force]

Remove (or simulate removal of) \`.worktrees/*\` worktrees whose branch is
fully merged into main. Refuses ahead-of-main, detached, bare, or dirty
(without --force) worktrees. After processing, runs
\`git remote prune origin\` to drop stale tracking refs.

Options:
  --dry-run   Print actions without executing removal or branch deletion.
  --force     Allow removal even when the worktree has dirty files.
  -h --help   Show this message.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --force)   FORCE=1;   shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$LOG_PREFIX ERROR: unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

cd "$REPO_ROOT"

log()  { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
warn() { printf '%s WARN  %s\n' "$LOG_PREFIX" "$*" >&2; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "$LOG_PREFIX ERROR: not inside a git repository" >&2
    exit 1
}

# Resolve main branch (prefer 'main'; fall back to 'master' if needed).
MAIN_BRANCH=main
if ! git show-ref --verify --quiet "refs/heads/$MAIN_BRANCH"; then
    if git show-ref --verify --quiet refs/heads/master; then
        MAIN_BRANCH=master
    else
        echo "$LOG_PREFIX ERROR: no 'main' or 'master' branch found locally" >&2
        exit 1
    fi
fi
log "main branch: $MAIN_BRANCH"
[ "$DRY_RUN" -eq 1 ] && log "mode: dry-run (no removal or branch deletion)"
[ "$FORCE"   -eq 1 ] && log "mode: --force (dirty worktrees will be removed)"

# Refresh remote refs so --merged checks are accurate.
log "fetching --prune origin (best effort)"
if ! git fetch --prune origin 2>&1 | sed "s/^/$LOG_PREFIX (fetch) /"; then
    warn "git fetch --prune origin failed; continuing with local refs only"
fi

# Walk `git worktree list --porcelain` records.
# Each record is a sequence of `key value` lines (or bare `bare` / `detached`)
# terminated by a blank line; the very last record may not have one, so we
# add an explicit newline.
wt_path=""
wt_branch=""
wt_extras=""
removed=()
kept=()
refused=()

flush_record() {
    [ -n "$wt_path" ] || return 0
    case " $wt_extras " in
        *" pruned "*)
            log "SKIP $(basename "$wt_path") (git-flagged prunable entry — run \`git worktree prune\` first)"
            ;;
        *" detached "*)
            log "SKIP $(basename "$wt_path") (detached HEAD — no branch to remove)"
            refused+=("$(basename "$wt_path"):detached")
            ;;
        *" bare "*)
            log "SKIP $(basename "$wt_path") (bare worktree)"
            refused+=("$(basename "$wt_path"):bare")
            ;;
        *)
            process_worktree "$wt_path" "$wt_branch"
            ;;
    esac
    wt_path=""; wt_branch=""; wt_extras=""
}

process_worktree() {
    local path="$1"
    local branch_ref="$2"
    local name; name=$(basename "$path")

    # 1) skip the main checkout (== repo root / current worktree).
    if [ "$(cd "$path" && pwd -P)" = "$(pwd -P)" ]; then
        log "SKIP $name (== repo root)"
        return
    fi

    local branch="${branch_ref#refs/heads/}"
    local ahead behind dirty
    ahead=$(git -C "$path" rev-list --count "$MAIN_BRANCH"..HEAD 2>/dev/null || echo "?")
    behind=$(git -C "$path" rev-list --count HEAD.."$MAIN_BRANCH" 2>/dev/null || echo "?")
    dirty=$(git -C "$path" status --porcelain 2>/dev/null | wc -l || echo "?")

    log "name=$name branch=$branch ahead=$ahead behind=$behind dirty=$dirty"

    # 2) safety: branch is ahead of main.
    if [ "$ahead" != "0" ] && [ "$ahead" != "?" ]; then
        log "REFUSE $name — branch $branch is $ahead commits ahead of $MAIN_BRANCH"
        refused+=("$name:ahead=$ahead")
        return
    fi

    # 3) safety: dirty (override with --force).
    if [ "$dirty" != "0" ] && [ "$FORCE" -ne 1 ]; then
        log "REFUSE $name — $dirty dirty files; pass --force to override"
        refused+=("$name:dirty=$dirty")
        return
    fi

    # 4) safety: branch actually in `merged $MAIN_BRANCH` list.
    if ! git branch --merged "$MAIN_BRANCH" --list "$branch" | grep -q .; then
        log "KEEP  $name — branch $branch not in 'merged $MAIN_BRANCH' yet"
        kept+=("$branch:not-merged")
        return
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY   $name — would run: git worktree remove $path --force"
        log "DRY   $name — would run: git branch -d $branch"
        removed+=("$branch")
        return
    fi

    if git worktree remove "$path" --force; then
        log "REMOVED  worktree $path"
        if git branch -d "$branch" >/dev/null 2>&1; then
            log "DELETED  branch $branch"
            removed+=("$branch")
        else
            warn "branch $branch could not be deleted (already gone?)"
        fi
    else
        warn "git worktree remove $path --force failed"
        refused+=("$name:wt-remove-failed")
    fi
}

# Parse the porcelain output.
while IFS= read -r line; do
    case "$line" in
        worktree\ *) wt_path="${line#worktree }" ;;
        branch\ *)   wt_branch="${line#branch }" ;;
        HEAD\ *)     : ;;
        bare)        wt_extras="$wt_extras bare" ;;
        detached)    wt_extras="$wt_extras detached" ;;
        prunable\ *) wt_extras="$wt_extras pruned" ;;
        '')          flush_record ;;
        *) : ;;   # unknown field, ignore
    esac
done < <(git worktree list --porcelain; printf '\n')
flush_record

# Drop stale remote tracking refs.
log "git remote prune origin"
git remote prune origin 2>&1 | sed "s/^/$LOG_PREFIX (remote prune) /" || warn "git remote prune origin failed"

log "===== summary ====="
log "removed branches: ${removed[*]:-(none)}"
log "kept branches:    ${kept[*]:-(none)}"
log "refused entries:  ${refused[*]:-(none)}"
