#!/usr/bin/env bash
# Commit new data and health files in the data-branch checkout, then push, retrying if another
# job pushed first. Jobs write to separate files, so rebasing onto another job's commit is clean.
set -euo pipefail

msg="${1:?usage: commit_with_retry.sh <message> [dir] [branch]}"
dir="${2:-store}"
branch="${3:-data}"

cd "$dir"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for path in data site; do
  if [ -e "$path" ]; then git add --sparse -A "$path"; fi
done

if git diff --cached --quiet 2>/dev/null; then
  echo "No changes to commit."
  exit 0
fi

git commit -q -m "$msg"

for attempt in 1 2 3; do
  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    if ! git pull --rebase -q origin "$branch"; then
      git rebase --abort 2>/dev/null || true
      echo "Rebase failed on attempt ${attempt}; retrying." >&2
      sleep $((attempt * 5))
      continue
    fi
  fi
  if git push -q origin "HEAD:${branch}"; then
    echo "Pushed on attempt ${attempt}."
    exit 0
  fi
  echo "Push failed on attempt ${attempt}; retrying." >&2
  sleep $((attempt * 5))
done

echo "Could not push after 3 attempts." >&2
exit 1
