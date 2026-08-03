#!/usr/bin/env bash
# 25-npm-install.sh — Install apps/mewbo_console's node_modules.
#
# create-only, same reasoning as 20-uv-sync.sh: `npm ci` is the expensive step
# and is itself the idempotent/reproducible install (it deletes and rebuilds
# node_modules from package-lock.json), so a 'start' re-run buys nothing.
#
# NOTE: mewbo_console's own `postinstall` runs `fetch-pyodide.mjs`, which pulls
# Pyodide over the network — already called out as an egress consideration in
# the frozen contract (assistant's `container.egress.allow`, set by the
# devcontainer.json owner). This script does not special-case it; a failure
# there is `npm ci`'s to report, not ours to swallow.
[ "${GROVE_INIT_PHASE}" = "create" ] || exit 0
set -euo pipefail

console_dir="${REPO_ROOT}/apps/mewbo_console"

if ! command -v npm >/dev/null 2>&1; then
	echo "-- skipped: npm not on PATH (expected from the devcontainer image/features)"
	exit 0
fi

if [ ! -f "${console_dir}/package.json" ]; then
	echo "-- skipped: ${console_dir}/package.json not found"
	exit 0
fi

(cd "$console_dir" && npm ci)
echo "-- npm ci complete in apps/mewbo_console"
