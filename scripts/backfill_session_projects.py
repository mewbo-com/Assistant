#!/usr/bin/env python3
"""Backfill the ``projects`` facet onto sessions that predate it.

A session's project SET (``mewbo_core.session.session_store.SessionStoreBase.
record_project``) is normally maintained incrementally by a hook on every
``context`` event append, so it is complete for every session created after
that hook shipped. A session created before it carries no ``projects`` at
all, and is invisible to a project filter until this runs once.

Driver-agnostic by construction: it goes through ``create_session_store()``,
the same factory the runtime uses, so one run covers whichever backend
``storage.driver`` selects (``json`` or ``mongodb``) rather than assuming
Mongo the way the wiki backfills do.

Properties:
- **Idempotent** — replays each session's own ``context`` events through the
  SAME derivation the live hook uses (``ProjectIdentity.from_context`` +
  ``record_project``), which is itself a no-op once a project is already
  recorded. Re-running finds nothing new to add.
- **Read-only unless writing** — never runs during a listing (a live store
  write on a read path is the exact tradeoff the design rejected); this is
  the one place that tradeoff is deliberately made, once, offline.
- **Un-attributable sessions are left alone, not guessed** — a session whose
  transcript names no project (a bare temp dir, or one that never left the
  ``auto`` sentinel) gets no ``projects`` key, same as a session created
  today under the same conditions.
"""

from __future__ import annotations

import argparse
import sys

from mewbo_core.session.session_store import create_session_store
from mewbo_core.workspaces.project_identity import ProjectIdentity


def main() -> int:
    """Replay every session's context events through the live derivation rule."""
    parser = argparse.ArgumentParser(
        description="Backfill the `projects` facet onto sessions that predate it."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be recorded without writing anything.",
    )
    args = parser.parse_args()

    store = create_session_store()

    touched = 0
    unchanged = 0
    for session_id in store.list_sessions():
        already = set(store.projects_for_session(session_id))
        found: set[str] = set()
        for event in store.load_transcript(session_id):
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            project = ProjectIdentity.from_context(payload)
            if project is not None:
                found.add(project)

        new = found - already
        if not new:
            unchanged += 1
            continue

        touched += 1
        print(f"{session_id}: +{sorted(new)}")
        if not args.dry_run:
            for project in new:
                store.record_project(session_id, project)

    verb = "would touch" if args.dry_run else "touched"
    print(f"\ndone: {verb} {touched} session(s), {unchanged} already complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
