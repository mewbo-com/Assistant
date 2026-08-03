#!/usr/bin/env bash
# 10-git-setup.sh — Non-interactive git config for container use.
#
# ADAPTED from docker/init.d/10-git-setup.sh. Deliberately DROPPED: this script
# sets no user.name/user.email. Grove itself forwards those (and the rest of a
# curated git config) into the container per-process via
# GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n env vars, and
# deliberately does not mount ~/.gitconfig (frozen contract §0). Writing
# identity here would just be redundant with, or fight, that mechanism. What's
# left — credential storage, the optional GitHub bridge, and trusting
# bind-mounted worktrees — has no Grove equivalent, so it stays.
#
# Runs in BOTH phases: writing ~/.gitconfig entries is cheap and idempotent
# (git config is itself idempotent — setting the same value twice is a no-op).
set -euo pipefail

# Host-agnostic: git reads credentials for ANY host from a mounted store, if
# one is mounted. No store, no helper — an unauthenticated container must
# still start cleanly.
if [ -f "${HOME}/.git-credentials" ]; then
	git config --global credential.helper store
	echo "-- git credential.helper=store (found ~/.git-credentials)"
else
	echo "-- skipped: credential.helper (no ~/.git-credentials mounted)"
fi

# Optional GitHub convenience bridge, additive and scoped so it never shadows
# credentials for other hosts (Gitea, GitLab, ...) configured above.
if command -v gh >/dev/null 2>&1 && [ -n "${GITHUB_TOKEN:-}" ]; then
	git config --global credential.https://github.com.helper '!gh auth git-credential'
	echo "-- git credential bridge: gh auth git-credential for github.com"
else
	echo "-- skipped: gh credential bridge (gh absent or GITHUB_TOKEN unset)"
fi

# The workspace worktree (and any sibling worktrees under the same repo) is a
# bind mount owned by the host uid, which commonly differs from the container
# user. Without this, git refuses every command with "detected dubious
# ownership".
git config --global safe.directory '*'
