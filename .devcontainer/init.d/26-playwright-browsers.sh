#!/usr/bin/env bash
# 26-playwright-browsers.sh — Install the Chromium build apps/mewbo_console's
# Playwright suite needs.
#
# Chromium only — playwright.config.ts / playwright.demo.config.ts exercise a
# single browser, and `@playwright/test` in package.json is pinned by npm's
# `^1.58.2` range, since `npx playwright` always resolves the LOCALLY installed
# version rather than whatever the container happens to have on PATH.
#
# The image deliberately bakes NO browser (Dockerfile: "Playwright ... a ~2 GB
# layer no gate command in this repo needs"), so this step is what actually
# populates PLAYWRIGHT_BROWSERS_PATH (docker-compose.yml: /caches/playwright,
# the shared volume). Playwright keys its browser directory off the EXACT
# installed playwright-core version, so do NOT short-circuit on "some
# chromium-* directory already exists" — that check is coarser than the thing
# it would be caching and turns a real drift into a silent failure.
# `playwright install` is itself idempotent and exits in well under a second
# when the exact build is already present, so always let it decide instead.
#
# create-only, same reasoning as 20-uv-sync.sh/25-npm-install.sh: the download
# is the expensive step, and re-running it on 'start' buys nothing once the
# shared cache volume has the build.
#
# --with-deps: the image's apt layer (Dockerfile) carries none of Chromium's
# runtime shared libraries (libnss3, libgtk, ...) since it bakes no browser
# either. `playwright install --with-deps` shells out to `apt-get` for those
# via sudo when not run as root, which the mewbo user has passwordless.
[ "${GROVE_INIT_PHASE}" = "create" ] || exit 0
set -euo pipefail

console_dir="${REPO_ROOT}/apps/mewbo_console"

if [ ! -d "$console_dir" ]; then
	echo "-- skipped: apps/mewbo_console not found"
	exit 0
fi

cd "$console_dir"
if [ ! -d node_modules ]; then
	echo "-- skipped: node_modules missing, 25-npm-install.sh must run first"
	exit 0
fi

echo "-- npx playwright install --with-deps chromium"
npx playwright install --with-deps chromium
