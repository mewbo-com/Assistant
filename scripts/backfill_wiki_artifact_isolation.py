#!/usr/bin/env python3
"""Backfill ``commit_sha``/``job_id`` onto pre-isolation wiki artifact rows.

Graph nodes/edges/embeddings, entities/entity-edges/entity-embeddings and pages
gained per-job/commit attribution so a completed re-index can supersede the prior
commit's artifacts instead of unioning into them. Rows written before that carry
neither field. This script stamps them so the machinery has something to reason
about.

Runs against the deployed Mongo (same env vars as ``backfill_wiki_snapshot.py``).
The decision + write logic lives in ``mewbo_graph.wiki.backfill`` so it is unit
tested without a live database; this file only wires the collections.

Properties (see ``ArtifactIsolationBackfill``):
- **Idempotent** — every write is gated on ``commit_sha {$exists: false}``, so a
  re-run stamps nothing. Safe to run repeatedly.
- **Non-destructive** — it only ``$set``s two fields; it never deletes. Reaping
  the union is the next index's ``supersede_graph_artifacts``, not this script's.
- **A slug already carrying isolated rows gets a reap sentinel, not a guess** —
  its remaining field-absent rows predate the job that produced those isolated
  rows, so guessing they belong to that job's commit would be a wrong write.
  See ``mewbo_graph.wiki.backfill``'s module docstring for why.
- **Un-attributable rows are marked, not guessed** — a slug whose jobs never
  resolved a commit (a catalog workspace, or a clone that never succeeded) has
  its rows stamped ``None``/``None``, which supersede then preserves.

``--dry-run`` reports exactly what would be stamped without writing anything —
run this first. This script never deletes and a dry run never writes at all,
but the real run does mutate a live database; confirm the report before
dropping the flag.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mewbo_graph.wiki.backfill import ARTIFACT_COLLECTIONS, ArtifactIsolationBackfill
from pymongo import MongoClient

_STRATEGY_LABELS = {
    "job_guess": "attributed to commit {commit_short} (job {job_id})",
    "isolation_sentinel": (
        "already has isolated rows — legacy rows stamped with the reap "
        "sentinel, dropped by the next supersede regardless of commit"
    ),
    "unattributable": "un-attributable — stamped null (preserved by supersede)",
}


def main() -> int:
    """Run the backfill (or preview it with ``--dry-run``) against the deployed Mongo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be stamped without writing anything.",
    )
    args = parser.parse_args()

    uri = os.environ.get(
        "MEWBO_MONGODB_URI", "mongodb://mewbo:mewbo@localhost:27017/?authSource=admin"
    )
    database = os.environ.get("MEWBO_MONGODB_DATABASE", "mewbo")
    client: MongoClient[dict[str, Any]] = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[database]

    backfill = ArtifactIsolationBackfill(
        jobs=db["wiki_jobs"],
        artifacts={name: db[name] for name in ARTIFACT_COLLECTIONS},
    )
    report = backfill.run(dry_run=args.dry_run)
    suffix = " [DRY RUN — nothing written]" if args.dry_run else ""

    for slug, decision in sorted(report["slugs"].items()):
        label = _STRATEGY_LABELS[decision["strategy"]]
        if decision["strategy"] == "job_guess":
            label = label.format(
                commit_short=decision["commit_sha"][:12], job_id=decision["job_id"]
            )
        print(f"{slug}: {label}{suffix}")

    verb = "would stamp" if args.dry_run else "stamped"
    print(f"\nper-collection rows {verb}:")
    for name, count in sorted(report["collections"].items()):
        print(f"  {name}: {count}")
    print(f"\ndone: {report['total_modified']} rows {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
