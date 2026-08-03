#!/usr/bin/env bash
# 80-readiness-check.sh — Non-fatal summary of toolchain and reachability.
#
# NEVER exits non-zero: bootstrap.sh aborts the whole phase on any script's
# non-zero exit, and a slow/absent optional service must not block a
# container's startup over a report line. Every check below is a best-effort
# probe with its own short timeout, printed pass/fail, and no `set -e` around
# the probes themselves.
#
# "internal services" are found generically, not by name: this scans the
# environment for *_API_BASE / *_BASE_URL / *_ENDPOINT suffixed vars
# (OPENAI_API_BASE being the canonical example) rather than hardcoding a
# hostname — per the repo's no-policy-in-code rule, this script supplies
# the mechanism, the actual endpoints are whatever devcontainer.json's
# containerEnv or the host's real environment name.
set -uo pipefail
[ "${GROVE_INIT_PHASE}" = "start" ] || [ "${GROVE_INIT_PHASE}" = "create" ] || exit 0

echo "== readiness check =="

for tool_cmd in "python3 --version" "uv --version" "node --version" "npm --version" "tea --version"; do
	tool="${tool_cmd%% *}"
	if command -v "$tool" >/dev/null 2>&1; then
		version="$($tool_cmd 2>&1 | head -n1)"
		echo "pass: ${tool} (${version})"
	else
		echo "fail: ${tool} not on PATH"
	fi
done

# Resolves the SAME way a real test does (`chromium.executablePath()` off the
# locally installed playwright-core) and confirms the binary is actually on
# disk in the shared cache volume, rather than trusting a directory listing —
# 26-playwright-browsers.sh's whole point is that the two can disagree.
console_dir="${REPO_ROOT}/apps/mewbo_console"
if [ -d "$console_dir" ] && [ -d "$console_dir/node_modules" ]; then
	if chromium_path="$(cd "$console_dir" && node -e "console.log(require('playwright-core').chromium.executablePath())" 2>&1)" \
		&& [ -x "$chromium_path" ]; then
		echo "pass: playwright chromium resolvable (${chromium_path})"
	else
		echo "fail: playwright chromium not resolvable (${chromium_path:-node -e failed})"
	fi
else
	echo "-- skipped: apps/mewbo_console/node_modules not present"
fi

if command -v docker >/dev/null 2>&1; then
	if docker info >/dev/null 2>&1; then
		echo "pass: nested dockerd reachable ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
	else
		# dockerd is started by the docker-in-docker feature's entrypoint and can
		# still be coming up when `start` runs, so this is a report, not a verdict.
		echo "fail: docker CLI present but the nested daemon is not up yet"
	fi
else
	echo "fail: docker not on PATH"
fi

# This stack's own Mongo, not the host's. Absent is a legitimate state (the
# pytest suite is hermetic and needs no database), so this reports and moves on.
mongo_host="$(printf '%s' "${MEWBO_MONGODB_URI:-}" | sed -E 's#^mongodb://##; s#[/?].*##; s#:.*##')"
if [ -n "$mongo_host" ]; then
	if (exec 3<>"/dev/tcp/${mongo_host}/27017") 2>/dev/null; then
		echo "pass: mongo reachable at ${mongo_host}:27017"
	else
		echo "fail: mongo not reachable at ${mongo_host}:27017"
	fi
else
	echo "-- skipped: MEWBO_MONGODB_URI not set"
fi

checked_any=0
while IFS='=' read -r name _; do
	case "$name" in
	*_API_BASE | *_BASE_URL | *_ENDPOINT)
		checked_any=1
		value="${!name}"
		[ -n "$value" ] || continue
		host="$(printf '%s' "$value" | sed -E 's#^[a-zA-Z]+://##; s#[/:].*##')"
		[ -n "$host" ] || continue
		if getent ahostsv4 "$host" >/dev/null 2>&1; then
			echo "pass: ${name} host '${host}' resolves"
		else
			echo "fail: ${name} host '${host}' does not resolve"
		fi
		;;
	esac
done < <(env)

if [ "$checked_any" -eq 0 ]; then
	echo "-- skipped: no *_API_BASE / *_BASE_URL / *_ENDPOINT env vars set"
fi

exit 0
