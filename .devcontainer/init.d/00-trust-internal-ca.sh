#!/usr/bin/env bash
# 00-trust-internal-ca.sh — Trust an internal/self-signed root CA, if provided.
#
# ADAPTED from docker/init.d/05-trust-internal-ca.sh: same trust-then-point-env
# logic, but the SOURCE differs. The production script assumes a compose mount
# always drops the PEM at /usr/local/share/ca-certificates/*.crt; a devcontainer
# has no such guarantee (no docker-compose.override.yml here), so this script
# also accepts a path via $INTERNAL_CA_FILE (set through devcontainer.json's
# containerEnv or an ad hoc mount) and copies it into place first. Neither
# source is required — most devs have no internal CA, and the container must
# still come up clean without one (contract: optional-resource absence is
# never fatal).
#
# Runs in BOTH phases: cheap (a handful of stat calls + an idempotent
# update-ca-certificates), and 'start' must re-apply it because /etc/environment
# writes (below) do not survive a container recreate the way a volhome would.
set -euo pipefail

CERT_DIR=/usr/local/share/ca-certificates

if [ -n "${INTERNAL_CA_FILE:-}" ] && [ -f "${INTERNAL_CA_FILE}" ]; then
	# update-ca-certificates only picks up *.crt — the extension is load-bearing.
	dest="${CERT_DIR}/internal-ca.crt"
	if [ ! -f "$dest" ] || ! cmp -s "${INTERNAL_CA_FILE}" "$dest"; then
		sudo mkdir -p "$CERT_DIR"
		sudo cp "${INTERNAL_CA_FILE}" "$dest"
	fi
fi

if ! ls "${CERT_DIR}"/*.crt >/dev/null 2>&1; then
	echo "-- skipped: no internal CA at \$INTERNAL_CA_FILE or ${CERT_DIR}/*.crt"
	exit 0
fi

sudo update-ca-certificates >/dev/null 2>&1 || true

bundle=/etc/ssl/certs/ca-certificates.crt

# requests/httpx (Assistant's own HTTP stack) default to certifi's bundle, NOT
# the system trust store, so trusting the CA above is not enough on its own —
# point the Python and git env vars at the combined system bundle too.
# Verification stays ON everywhere; never set GIT_SSL_NO_VERIFY.
#
# These are exported for THIS script only — a devcontainer runs bootstrap.sh as
# a one-shot postCreate/postStart process, not sourced into the shell the
# developer later attaches to, so an `export` here would not reach that shell.
# /etc/environment is the persistence mechanism PAM-based login shells (and VS
# Code's devcontainer terminal) read on start. Idempotent: replace our marked
# block rather than appending duplicates on every 'start'.
marker_begin="# BEGIN devcontainer-internal-ca (managed by 00-trust-internal-ca.sh)"
marker_end="# END devcontainer-internal-ca"
tmp="$(mktemp)"
if [ -f /etc/environment ]; then
	sed "/^${marker_begin}\$/,/^${marker_end}\$/d" /etc/environment >"$tmp"
else
	: >"$tmp"
fi
{
	cat "$tmp"
	echo "$marker_begin"
	echo "SSL_CERT_FILE=${bundle}"
	echo "REQUESTS_CA_BUNDLE=${bundle}"
	echo "GIT_SSL_CAINFO=${bundle}"
	echo "$marker_end"
} | sudo tee /etc/environment >/dev/null
rm -f "$tmp"

echo "-- trusted internal CA; SSL_CERT_FILE/REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO written to /etc/environment"
