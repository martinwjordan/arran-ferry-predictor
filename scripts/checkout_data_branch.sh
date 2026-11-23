#!/usr/bin/env bash
# Check out the `data` branch into <dir>, or start it if it doesn't exist yet.
# Sparse and shallow: jobs only read state files and health, so they never download the archive.
set -euo pipefail

dir="${1:?usage: checkout_data_branch.sh <dir>}"
url="https://x-access-token:${GITHUB_TOKEN:?GITHUB_TOKEN not set}@github.com/${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}.git"

if git ls-remote --exit-code --heads "$url" data >/dev/null 2>&1; then
  git clone --quiet --depth 1 --branch data --filter=blob:none --sparse "$url" "$dir"
  git -C "$dir" sparse-checkout set data/state site
  echo "Checked out the data branch (state and health only)."
else
  git init -q -b data "$dir"
  git -C "$dir" remote add origin "$url"
  echo "No data branch yet; starting one."
fi
