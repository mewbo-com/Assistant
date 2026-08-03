#!/bin/sh
# entrypoint.sh — Shared runtime init for the Python images, then exec the CMD.
#
# Two images use this (api, mcp); the command they start is their own CMD, so
# this file owns only what is common: writable-path repair, then the ordered
# init.d pass. Anything image-specific belongs in a CMD or an init.d script,
# never here.
#
# Scans INIT_DIR for *.sh scripts (sorted by filename for ordering) and SOURCES
# each one — sourcing, not executing, is load-bearing: it is what lets a script
# export an env var (SSL_CERT_FILE, PATH additions) that the app inherits.
# A failing script logs a warning but does NOT prevent startup.
#
# The images bake NO init scripts. docker-compose.yml bind-mounts the whole
# docker/init.d directory read-only, which is what makes runtime setup editable
# without a rebuild. Add one by dropping a file in that directory; a numeric
# prefix places it in the order (00 toolbox helpers, 1x identity, then yours).
# A single extra script can also be mounted on its own:
#   volumes:
#     - ./my-init.sh:/app/docker/init.d/20-my-init.sh:ro
#
# A missing INIT_DIR is therefore normal, not a fault — an image run with no
# mount (the demo stack, a bare `docker run`) simply performs no init.

set -u

# Named Docker volumes AND bind mounts whose host directory was auto-created by
# docker mount as root:root even when the container runs as a non-root user.
# Fix ownership of writable directories that need it. The mewbo user has
# passwordless sudo (see Dockerfile.base).
#
# Image-specific, because the mcp image has none of the api's scratch volumes:
# each Dockerfile sets INIT_CHOWN_DIRS to the paths it actually mounts, so mcp
# does not warn about api-only directories that will never exist there.
# Web IDE deadline files are NOT in this list any more: the directory holding
# them moved to the mewbo-ide broker, which is a plain Node image and does not
# run this entrypoint at all. Neither image that does run it touches the docker
# socket now.
for _dir in ${INIT_CHOWN_DIRS:-}; do
    if [ -d "$_dir" ] && [ ! -w "$_dir" ]; then
        printf '[entrypoint] Fixing ownership on %s\n' "$_dir"
        sudo chown -R "$(id -u):$(id -g)" "$_dir"
    fi
done

INIT_DIR="${INIT_DIR:-/app/docker/init.d}"

if [ -d "$INIT_DIR" ]; then
    for f in $(find "$INIT_DIR" -maxdepth 1 -name '*.sh' -type f 2>/dev/null | sort -V); do
        if [ -r "$f" ]; then
            printf '[init] %s ...' "$(basename "$f")"
            set +e
            . "$f"
            rc=$?
            set -u
            if [ $rc -ne 0 ]; then
                printf ' FAILED (exit %d, continuing)\n' "$rc" >&2
            else
                printf ' ok\n'
            fi
        fi
    done
fi

printf '[entrypoint] Starting: %s\n' "$*"
exec "$@"
