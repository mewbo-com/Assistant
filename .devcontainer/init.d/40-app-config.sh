#!/usr/bin/env bash
# 40-app-config.sh — Seed configs/app.json from configs/app.example.json.
#
# configs/app.json is gitignored (**/app.json in .gitignore, with an explicit
# !configs/*.example.json carve-out for the template), so a fresh Grove
# worktree — which is a plain git checkout of tracked content — never has one.
# Unlike .mcp.json/CLAUDE.local.md (seeded host-side by .grove/init.sh from the
# MAIN repo, before the container ever starts), app.json has no host copy to
# inherit: it is genuinely per-workspace runtime config, not shared identity,
# so copying the tracked example is the right seed rather than reaching out to
# the main repo's live app.json.
#
# Never overwrites an existing app.json — a developer's edits (or a copy from
# a previous 'create') always win.
#
# Both phases: the existence check makes 'start' a no-op once seeded, and
# re-checking is cheap enough not to bother gating to 'create' only.
set -euo pipefail

app_json="${REPO_ROOT}/configs/app.json"
example="${REPO_ROOT}/configs/app.example.json"

if [ -f "$app_json" ]; then
	echo "-- skipped: configs/app.json already present"
	exit 0
fi

if [ ! -f "$example" ]; then
	echo "-- skipped: configs/app.example.json not found"
	exit 0
fi

cp "$example" "$app_json"
echo "-- seeded configs/app.json from configs/app.example.json"
