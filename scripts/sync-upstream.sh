#!/usr/bin/env bash
# Rebase the ryderdavid/hermes-agent fork onto NousResearch/hermes-agent.
#
#   scripts/sync-upstream.sh [--branch NAME] [--push] [--skip-checks]
#
# 1. fetches `upstream` and fast-forwards local `main` to upstream/main,
# 2. rebases the work branch (default: current branch) onto it,
# 3. runs a smoke test subset (pytest -k "tool_progress or session_history or display"),
# 4. on rebase conflict, stops and files a bead so the integration is tracked.
#
# Nothing is pushed unless --push is given; then `main` is pushed and the work
# branch is force-pushed with lease. See docs/FORK.md.
set -euo pipefail

UPSTREAM_URL="https://github.com/NousResearch/hermes-agent.git"
branch=""
push=false
checks=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) branch="$2"; shift 2 ;;
    --push) push=true; shift ;;
    --skip-checks) checks=false; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Working tree has uncommitted changes; commit or stash first." >&2
  exit 1
fi

branch="${branch:-$(git rev-parse --abbrev-ref HEAD)}"
if [[ "$branch" == "main" || "$branch" == "HEAD" ]]; then
  echo "Check out the fork's work branch (or pass --branch) first." >&2
  exit 1
fi

git fetch --quiet upstream || { echo "fetch upstream failed" >&2; exit 1; }

# Fast-forward main to upstream/main, then rebase the work branch onto it.
git checkout --quiet main
git merge --ff-only upstream/main
git checkout --quiet "$branch"

if ! git rebase main; then
  echo "Rebase conflicted. Resolve it, then run: git rebase --continue" >&2
  if command -v bd >/dev/null 2>&1; then
    bd create "Upstream sync conflict: $branch onto upstream/main" \
      -d "scripts/sync-upstream.sh hit a rebase conflict integrating upstream/main into '$branch'. Resolve the conflict, run the smoke subset (pytest -k \"tool_progress or session_history or display\"), then scripts/sync-upstream.sh --push. Fork customizations at risk: semantic tool labels in tui_gateway event payloads, session.info fork marker (docs/FORK.md)." \
      -t task -p 1 >/dev/null 2>&1 && echo "Filed bead for the conflict." >&2
  fi
  exit 1
fi

if [[ "$checks" == true ]]; then
  PY="${PYTHON:-}"
  if [[ -z "$PY" ]]; then
    if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python
    elif command -v pytest >/dev/null 2>&1; then PY="python3 -m"
    else echo "pytest not found; rerun with --skip-checks or set PYTHON=" >&2; exit 1; fi
  fi
  # shellcheck disable=SC2086
  $PY -m pytest tests/ -q -x -k "tool_progress or session_history or display" || {
    echo "Smoke tests failed after rebase; see above. File a bead if the failure touches fork customizations." >&2
    exit 1
  }
fi

if [[ "$push" == true ]]; then
  git push origin main
  git push --force-with-lease origin "$branch"
fi

echo "Synced: $branch is now on top of upstream/main ($(git rev-parse --short upstream/main))."
