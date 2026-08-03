#!/usr/bin/env bash
# 20-uv-sync.sh — Install the Python workspace's locked dependency closure.
#
# `--all-groups` rather than CI's literal `--all-extras --group dev`: today
# `dev` is the ONLY entry under [dependency-groups] in pyproject.toml
# (verified against pyproject.toml and .github/workflows/lint.yml), so the two
# forms install identically. `--all-groups` is chosen anyway because it stays
# dev-complete if a second group is ever added — a devcontainer should always
# get everything a contributor might need, unlike CI which pins exactly one
# named group on purpose. `--all-extras` matches CI's flag.
#
# create-only: this is the expensive, network-bound step (whole workspace: all
# packages/* + apps/*), and `uv sync` is itself idempotent/incremental, so
# re-running it on every 'start' would just cost a few seconds re-checking a
# lockfile that hasn't moved — cheap, but still pure waste on every resume.
[ "${GROVE_INIT_PHASE}" = "create" ] || exit 0
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
	echo "-- skipped: uv not on PATH (expected from the devcontainer image/features)"
	exit 0
fi

cd "$REPO_ROOT"
uv sync --all-extras --all-groups
echo "-- uv sync --all-extras --all-groups complete"
