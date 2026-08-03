#!/usr/bin/env bash
# Grove workspace init — seeds the local, gitignored layer a fresh worktree
# never gets on its own.
#
# Grove runs this with cwd set to the NEW worktree, once on create and again on
# resume (`init_script.run_on_resume`). Wired up in .grove/config.json.
#
# Why this exists: CLAUDE.local.md and .mcp.json are both gitignored/untracked,
# so they are absent from every fresh worktree — including the ones Grove
# builds for a workspace. Without this copy, an agent in a Grove workspace
# starts with neither local context nor MCP server config. `pause` deletes the
# worktree and `resume` rebuilds it, so the copy must run on both.
#
# Never fails a workspace: a missing source file is normal (a machine that has
# no local guide, or hasn't set up .mcp.json) and must not block workspace
# creation, so this always exits 0.

set -uo pipefail

# Resolve the MAIN repo root, which is where the authoritative copy lives.
#
# Prefer $GROVE_REPO, but do NOT rely on it: Grove passes extra_env on create
# and NOT on resume, so on the resume path it is unset. `git rev-parse
# --git-common-dir` works from inside a worktree in both cases — it points at
# the main repo's .git, whose parent is the main working tree.
main_root="${GROVE_REPO:-}"
if [ -z "$main_root" ]; then
  common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || {
    echo "grove-init: not a git repository; skipping"
    exit 0
  }
  # --git-common-dir may return a relative path; anchor it to cwd before use.
  case "$common_dir" in
    /*) ;;
    *) common_dir="$PWD/$common_dir" ;;
  esac
  main_root="$(cd "$common_dir/.." && pwd)" || exit 0
fi

# Guard the degenerate case: a "workspace" rooted at the main repo would copy
# each file onto itself and truncate it.
if [ "$(pwd)" = "$main_root" ]; then
  echo "grove-init: workspace IS the main repo; nothing to copy"
  exit 0
fi

# Every gitignored/untracked file a fresh worktree needs seeded from the main
# repo. Add filenames here rather than pasting another near-identical block.
#
# configs/app.json is the load-bearing one and the easiest to overlook. It is
# gitignored, so a fresh worktree has only configs/app.example.json — which
# ships an EMPTY llm.api_base. The container would then come up perfectly
# healthy and still be unable to reach the model gateway, because the network
# was never the missing piece: the config was. Seeding it here (on the host
# worktree, which the dev container bind-mounts) is what keeps a containerized
# workspace from being a config-less one.
#
# .env is seeded too, since a dev container running its own docker daemon can
# bring the app's compose stack up and needs those vars to do it — but it is
# seeded FILTERED. That one file also carries host source-mount pointers
# (MEWBO_CONSOLE_DIST, MEWBO_API_SRC, MEWBO_CORE_SRC) naming absolute paths in
# the MAIN checkout. Copied verbatim into a worktree they would mount the main
# repo's source over its container, so the worktree would silently run the
# wrong tree's code. Dropping those lines lets the compose defaults — relative
# paths, which resolve inside the worktree — apply instead.
for name in CLAUDE.local.md .mcp.json configs/app.json .env; do
  src="$main_root/$name"
  dst="./$name"

  if [ ! -f "$src" ]; then
    echo "grove-init: no $name at $main_root; skipping"
    continue
  fi

  # Nested entries (configs/app.json) need their parent to exist. It normally
  # does — configs/ is tracked — but a seed must not depend on that.
  mkdir -p "$(dirname "$dst")" 2>/dev/null || true

  # See the MEWBO_*_SRC note above for why .env alone is filtered, not copied.
  if { [ "$name" = ".env" ] &&
         grep -v -E '^[[:space:]]*(MEWBO_CONSOLE_DIST|MEWBO_API_SRC|MEWBO_CORE_SRC)=' \
           "$src" > "$dst"; } || { [ "$name" != ".env" ] && cp "$src" "$dst"; }; then
    echo "grove-init: seeded $name from $main_root"
  else
    echo "grove-init: WARNING could not copy $name; continuing"
  fi
done

exit 0
