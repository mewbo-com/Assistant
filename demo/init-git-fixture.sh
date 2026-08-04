# Make ONE seeded managed project a real git repository, with a real worktree.
#
# `ProjectCard` mounts `WorktreesPanel` for every non-worktree project, and the
# panel asks `GET /v_projects/<id>/branches`. The seeded paths exist only as
# store records, so that call answers `git_repo: false` and every project card
# renders "Not a git repository, so worktrees are unavailable." The Workspace
# screenshot then shows a pane whose own description promises "the git
# worktrees it opens for parallel sessions" while four cards say worktrees are
# unavailable — and one of those cards is the parent of the seeded worktree row
# right below it. That self-contradiction is what this closes.
#
# Only `shipment-router` is made real, deliberately. It is the seeded
# worktree's parent, so making it a repository is what removes the
# contradiction; leaving the other three alone keeps the honest unavailable
# line in frame, and a capture showing BOTH states of the panel documents more
# than one showing either alone.
#
# Nothing here is faked. The repository, its commit, the branch and the
# worktree are all created with real git, at exactly the path and branch the
# seeder wrote into the store, so the API reports what actually exists on
# disk. `_merged_worktree_listing` dedupes the store record against the
# on-disk worktree by real path, so the row stays a single "managed" entry —
# and because the checkout genuinely exists, `WorktreeManager.is_clean`
# returns true instead of flagging the row "Uncommitted".
#
# All of this lives inside the container's own filesystem. `/workspaces` is not
# mounted from anywhere, so nothing here can reach the host tree, and a
# container recreate simply re-runs the script.
#
# Sourced (not executed) by docker/entrypoint.sh, so it must never call `exit`
# — that would terminate the entrypoint. The work runs in a subshell whose
# failure is contained, and the failure branch REMOVES the half-built
# directory so `_is_git_repo` goes back to false and the honest unavailable
# line returns. An init script's failure is non-fatal by design; degrading to
# the pre-existing state is the correct worst case.
DEMO_GIT_FIXTURE_REPO="${DEMO_GIT_FIXTURE_REPO:-/workspaces/acme/shipment-router}"
DEMO_GIT_FIXTURE_BRANCH="${DEMO_GIT_FIXTURE_BRANCH:-mewbo/main-9f2c1a}"
# Must equal `WorktreeManager.worktree_path(parent, branch)` — the same value
# the seeder stored as the worktree project's `path`. If they diverge, the
# merge stops deduping and the pane shows the worktree twice.
DEMO_GIT_FIXTURE_WORKTREE="${DEMO_GIT_FIXTURE_WORKTREE:-$DEMO_GIT_FIXTURE_REPO/.mewbo/worktrees/mewbo-main-9f2c1a}"

if [ -e "$DEMO_GIT_FIXTURE_REPO/.git" ]; then
    : # already built by an earlier start of this container; nothing to do
elif ! command -v git >/dev/null 2>&1; then
    printf '  (git unavailable; demo worktree fixture skipped)\n' >&2
else
    (
        set -e
        mkdir -p "$DEMO_GIT_FIXTURE_REPO"
        cd "$DEMO_GIT_FIXTURE_REPO"
        # `-b` pins the initial branch so the result does not depend on the
        # image's `init.defaultBranch`.
        git init -q -b main .
        # Repo-local identity: the image configures none, and a commit with no
        # author fails outright.
        git config user.email "demo@example.com"
        git config user.name "Mewbo Demo"
        printf 'Carrier selection service.\n' >README.md
        git add README.md
        # Fixed instants keep the commit — and therefore the whole repository —
        # reproducible across container restarts. Nothing renders the SHA
        # today; this just keeps it from becoming a surprise if something does.
        GIT_AUTHOR_DATE="2026-07-14T09:30:00Z" \
            GIT_COMMITTER_DATE="2026-07-14T09:30:00Z" \
            git commit -q -m "Add the service README"
        # The branch is created here rather than seeded, because a worktree
        # cannot check out a branch that does not exist.
        git worktree add -q -b "$DEMO_GIT_FIXTURE_BRANCH" "$DEMO_GIT_FIXTURE_WORKTREE"
    )
    if [ $? -ne 0 ]; then
        rm -rf "$DEMO_GIT_FIXTURE_REPO"
        printf '  (demo worktree fixture failed; worktrees stay unavailable)\n' >&2
    fi
fi
