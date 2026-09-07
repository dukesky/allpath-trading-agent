#!/usr/bin/env bash
# One-command deploy: Pro -> Air (production).
#
# Usage:
#   scripts/deploy-to-air.sh            # deploy current HEAD (must be pushed)
#   scripts/deploy-to-air.sh <rev>      # deploy a specific commit/tag/branch
#
# Safety model:
#   - Air is updated ONLY via `git fetch` + `git checkout --detach <rev>`.
#     Untracked production state (.env, *.db, memory/, strategies/, logs)
#     is never touched; checkout refuses to overwrite local modifications.
#   - Previous revision is recorded; on failed health check the script
#     rolls back and restarts automatically.
set -euo pipefail

AIR="tianzhang@100.119.178.89"
REPO="/Users/tianzhang/Projects/allpath-trading-agent"
LABEL="com.allpath.experiment-run"
UV="/opt/homebrew/bin/uv"
HEALTH_URL="http://localhost:8791/"
HEALTH_TIMEOUT=60

rev_input="${1:-HEAD}"
rev="$(git rev-parse --verify "${rev_input}^{commit}")"
echo "==> Deploying revision: $rev ($(git log --oneline -1 "$rev"))"

# The revision must be reachable from origin so Air can fetch it.
git fetch origin --quiet
if ! git merge-base --is-ancestor "$rev" origin/main 2>/dev/null \
   && ! git branch -r --contains "$rev" | grep -q .; then
  echo "ERROR: $rev is not pushed to origin. Push first, then deploy." >&2
  exit 1
fi

ssh -o BatchMode=yes "$AIR" REPO="$REPO" LABEL="$LABEL" UV="$UV" REV="$rev" \
    HEALTH_URL="$HEALTH_URL" HEALTH_TIMEOUT="$HEALTH_TIMEOUT" 'bash -s' <<'REMOTE'
set -euo pipefail
cd "$REPO"

prev="$(git rev-parse HEAD)"
echo "==> Air current revision: $prev"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: Air has uncommitted changes to tracked files; refusing to deploy." >&2
  git status --porcelain --untracked-files=no >&2
  exit 1
fi

git fetch origin --quiet
git checkout --detach --quiet "$REV"
echo "==> Checked out $REV"

echo "==> Syncing dependencies (uv sync)"
"$UV" sync --quiet

echo "==> Restarting $LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

health_ok() {
  for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
    code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
    case "$code" in 2*|3*) return 0 ;; esac
    sleep 1
  done
  return 1
}

if health_ok; then
  echo "==> Health check passed. Deployed $(git log --oneline -1)"
else
  echo "!! Health check FAILED. Rolling back to $prev" >&2
  git checkout --detach --quiet "$prev"
  "$UV" sync --quiet
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  if health_ok; then
    echo "!! Rolled back to $prev; service healthy again. Deploy of $REV failed." >&2
  else
    echo "!! ROLLBACK ALSO UNHEALTHY. Manual intervention needed on Air." >&2
  fi
  exit 1
fi
REMOTE

echo "==> Done."
