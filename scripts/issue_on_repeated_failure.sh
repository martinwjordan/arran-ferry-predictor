#!/usr/bin/env bash
# Open a GitHub issue when a workflow's last three runs have all failed, unless one is already open.
# GitHub emails the repo owner about new issues, so this is how persistent failures reach M.
set -euo pipefail

workflow="${1:?usage: issue_on_repeated_failure.sh <workflow file, e.g. collect.yml>}"
label="pipeline-failure"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"

# The current run may still show as in progress, so count it as failed explicitly.
recent=$(gh run list --repo "$repo" --workflow "$workflow" --limit 3 --json conclusion,status \
  --jq '[.[] | if .status != "completed" then "failure" else .conclusion end] | map(select(. == "failure")) | length')

if [ "$recent" -lt 3 ]; then
  echo "Only ${recent} of the last 3 ${workflow} runs failed; no issue."
  exit 0
fi

open=$(gh issue list --repo "$repo" --label "$label" --state open --search "$workflow in:title" --json number --jq 'length')
if [ "$open" -gt 0 ]; then
  echo "An open ${label} issue for ${workflow} already exists."
  exit 0
fi

gh label create "$label" --repo "$repo" --color B60205 --description "Scheduled job failing repeatedly" 2>/dev/null || true

runs=$(gh run list --repo "$repo" --workflow "$workflow" --limit 3 --json url --jq '.[].url' | sed 's/^/- /')
gh issue create --repo "$repo" --label "$label" \
  --title "${workflow} has failed three runs in a row" \
  --body "The last three runs of \`${workflow}\` failed. Data from this job is not being collected.

${runs}

Close this issue once the job is healthy again."
