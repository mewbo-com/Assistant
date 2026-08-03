#!/usr/bin/env bash
# 60-tea-cli.sh — Install tea (Gitea's official CLI), a static release binary.
#
# ADAPTED, narrowly, from docker/init.d/00-toolbox.sh + 12-agent-clis.sh:
# - gh (GitHub CLI) is DROPPED here — Grove's own devcontainer Features chain
#   already installs github-cli (frozen contract, Tier 1), so fetching it again
#   would just race a second copy onto PATH.
# - models (github.com/reyamira/models) is DROPPED — a nice-to-have pricing/
#   version browser, not something the repo's build or gates depend on; the
#   contract asks for conservatism, and this is the toolchain-adjacent call to
#   skip rather than install.
# - The generic multi-tool `mewbo_fetch_bin` helper in 00-toolbox.sh is NOT
#   reused as a shared file: it earns its abstraction in production by serving
#   THREE tools across TWO images; here there is exactly one tool and one
#   script, so inlining the fetch is less code for the same result (YAGNI).
# - tea's own runtime AUTHENTICATION (docker/init.d/15-tea-setup.sh, deriving
#   logins from configs/app.json's channels.vcs.tokens) is intentionally left
#   for the developer/agent to run by hand inside the container — this script
#   only gets the binary onto PATH.
#
# create-only + a `command -v` guard: a static binary that never needs to move
# once installed, so 'start' would only re-check work already done.
[ "${GROVE_INIT_PHASE}" = "create" ] || exit 0
set -euo pipefail

if command -v tea >/dev/null 2>&1; then
	echo "-- skipped: tea already on PATH"
	exit 0
fi

TEA_VERSION="${TEA_VERSION:-0.14.0}"

arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
case "$arch" in
x86_64) arch=amd64 ;;
aarch64) arch=arm64 ;;
esac

# dl.gitea.com is Gitea's object-store-backed release CDN — same binaries as
# the gitea.com web app's release-attachment URLs, but without an application
# server in the request path. That distinction mattered enough in production
# to earn its own comment (docker/init.d/12-agent-clis.sh): the web-app URL
# serves intermittent 5xx and once failed an image build outright.
url="https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${arch}"

dest_dir=/usr/local/bin
if [ ! -w "$dest_dir" ] && ! sudo -n true 2>/dev/null; then
	dest_dir="${HOME}/.local/bin"
fi
mkdir -p "$dest_dir"

tmp="$(mktemp)"
ok=0
for attempt in 1 2 3; do
	if curl -fsSL --connect-timeout 10 --max-time 120 "$url" -o "$tmp"; then
		ok=1
		break
	fi
	[ "$attempt" -lt 3 ] && sleep 3
done
if [ "$ok" -ne 1 ]; then
	echo "-- skipped: tea download failed after 3 tries (${url})"
	rm -f "$tmp"
	exit 0
fi

if [ -w "$dest_dir" ]; then
	install -m 0755 "$tmp" "${dest_dir}/tea"
else
	sudo install -m 0755 "$tmp" "${dest_dir}/tea"
fi
rm -f "$tmp"

echo "-- installed tea ${TEA_VERSION} to ${dest_dir}/tea"
