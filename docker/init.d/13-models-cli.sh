#!/bin/sh
# 13-models-cli.sh — Provision the `models` CLI/TUI into the runtime toolbox.
#
# models (github.com/reyamira/models, published to crates.io as `modelsdev`)
# browses AI models and their pricing, benchmark tables, tracked coding-agent
# versions, and provider status pages. Unlike gh and tea it carries no identity,
# so there is no companion auth script — install is the whole story, which is
# why this sits outside 12-agent-clis.sh rather than being appended to it.
#
# Release assets are named by Rust target triple, NOT the Debian amd64/arm64
# that `mewbo_arch` produces for gh and tea — so this reads `uname -m`, which
# already spells the arch the way the triple does (x86_64, aarch64).
#
# Needs glibc >= 2.39: every published linux-gnu build imports
# pidfd_spawnp/pidfd_getpid. The base image is Debian 13 (trixie, glibc 2.41),
# which satisfies it — that is precisely why the base moved off bookworm (2.36),
# where this binary installed cleanly and then died on first use. There is no
# runtime check here because there is nothing to choose between: one base image,
# and it meets the floor.

MODELS_VERSION="${MODELS_VERSION:-0.14.0}"

# The tarball holds a single flat `models` member with no directory prefix.
mewbo_fetch_bin models \
    "https://github.com/reyamira/models/releases/download/v${MODELS_VERSION}/models-$(uname -m)-unknown-linux-gnu.tar.gz" \
    "models"
