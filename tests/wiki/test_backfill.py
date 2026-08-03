"""Artifact-isolation backfill — attribution policy + idempotency.

The migration stamps ``commit_sha``/``job_id`` onto pre-isolation rows. Three
properties matter:

- the per-slug attribution DECISION, which now branches on whether the slug
  already carries isolated (real-commit) rows anywhere — see
  ``test_isolated_slug_...`` below for the case that used to misattribute;
- IDEMPOTENCY (a second run changes nothing);
- ``--dry-run`` (``run(dry_run=True)``) previews without writing.

None of the above needs a live Mongo: the ``ArtifactIsolationBackfill`` class
takes duck-typed collections, so a faithful in-memory fake exercises the real
``run``/``attribution_for``/``_has_isolated_rows`` code — the fake implements
only the Mongo operator subset the backfill uses (``$exists`` + equality),
kept minimal so it cannot pass by lying about a richer query.

Two further tests reproduce the ACTUAL predicates the sentinel must survive —
`supersede_graph_artifacts`'s Mongo ``$nin`` filter (``store.py``) and the JSON
driver's ``is None or == keep`` check — rather than reasoning about them, per
the house rule that a query's behaviour is tested, not described.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from mewbo_graph.wiki.backfill import (
    ARTIFACT_COLLECTIONS,
    LEGACY_ARTIFACT_SENTINEL,
    ArtifactIsolationBackfill,
)


class _FakeCollection:
    """In-memory stand-in for a Mongo collection (the ops the backfill uses)."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = [dict(d) for d in (docs or [])]

    def distinct(self, field: str, filt: dict | None = None) -> list:
        docs = self.docs if filt is None else [d for d in self.docs if self._match(d, filt)]
        return list({d[field] for d in docs if field in d})

    def find(self, filt: dict) -> list[dict]:
        return [dict(d) for d in self.docs if self._match(d, filt)]

    def count_documents(self, filt: dict) -> int:
        return sum(1 for d in self.docs if self._match(d, filt))

    def update_many(self, filt: dict, update: dict):
        n = 0
        for d in self.docs:
            if self._match(d, filt):
                d.update(update["$set"])
                n += 1
        return SimpleNamespace(modified_count=n)

    @staticmethod
    def _match(doc: dict, filt: dict) -> bool:
        for key, cond in filt.items():
            if isinstance(cond, dict) and "$exists" in cond:
                if (key in doc) != cond["$exists"]:
                    return False
            elif doc.get(key) != cond:
                return False
        return True


class _RecordingCollection(_FakeCollection):
    """A ``_FakeCollection`` that also records which methods were invoked.

    Subclassed rather than written afresh so a recorded run exercises exactly
    the matching semantics every other test here relies on — an independent
    fake could let an assertion pass by answering differently, which is the
    failure mode this whole file is built to avoid.
    """

    def __init__(self, docs: list[dict] | None = None) -> None:
        super().__init__(docs)
        self.calls: list[str] = []

    def distinct(self, field: str, filt: dict | None = None) -> list:
        self.calls.append("distinct")
        return super().distinct(field, filt)

    def find(self, filt: dict) -> list[dict]:
        self.calls.append("find")
        return super().find(filt)

    def count_documents(self, filt: dict) -> int:
        self.calls.append("count_documents")
        return super().count_documents(filt)

    def update_many(self, filt: dict, update: dict):
        self.calls.append("update_many")
        return super().update_many(filt, update)


def _artifacts(rows_by_slug: dict[str, int]) -> dict[str, _FakeCollection]:
    """Every artifact collection seeded with *rows_by_slug* legacy (no-commit) docs."""
    cols: dict[str, _FakeCollection] = {}
    for name in ARTIFACT_COLLECTIONS:
        docs: list[dict] = []
        for slug, count in rows_by_slug.items():
            docs.extend({"slug": slug, "node_id": f"{slug}-{i}"} for i in range(count))
        cols[name] = _FakeCollection(docs)
    return cols


# ── attribution policy (pure) ────────────────────────────────────────────────


def test_attribution_prefers_latest_completed_commit() -> None:
    jobs = [
        {
            "job_id": "j1",
            "status": "complete",
            "commit_sha": "old",
            "phase_started_at": "2026-01-01",
        },
        {
            "job_id": "j2",
            "status": "complete",
            "commit_sha": "new",
            "phase_started_at": "2026-02-01",
        },
        {
            "job_id": "j3",
            "status": "failed",
            "commit_sha": "newest",
            "phase_started_at": "2026-03-01",
        },
    ]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == ("new", "j2")


def test_attribution_falls_back_to_latest_attempt_with_a_commit() -> None:
    jobs = [
        {"job_id": "j1", "status": "failed", "commit_sha": "c1", "phase_started_at": "2026-01-01"},
        {
            "job_id": "j2",
            "status": "interrupted",
            "commit_sha": "c2",
            "phase_started_at": "2026-02-01",
        },
    ]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == ("c2", "j2")


def test_attribution_is_none_when_no_job_resolved_a_commit() -> None:
    jobs = [{"job_id": "j1", "status": "failed", "commit_sha": None}]
    assert ArtifactIsolationBackfill.attribution_for(jobs) == (None, None)


# ── run: stamps, then is idempotent ──────────────────────────────────────────


def test_backfill_stamps_derivable_and_marks_unattributable() -> None:
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
        # org/empty has no job that ever resolved a commit → un-attributable.
        {"job_id": "je", "slug": "org/empty", "status": "failed", "commit_sha": None},
    ])
    artifacts = _artifacts({"org/done": 3, "org/empty": 2})
    report = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    assert report["slugs"]["org/done"] == {
        "commit_sha": "deadbeef",
        "job_id": "jc",
        "strategy": "job_guess",
    }
    assert report["slugs"]["org/empty"] == {
        "commit_sha": None,
        "job_id": None,
        "strategy": "unattributable",
    }
    # Derivable rows carry the commit; un-attributable rows carry an explicit None.
    nodes = artifacts["wiki_graph_nodes"].docs
    done = [d for d in nodes if d["slug"] == "org/done"]
    empty = [d for d in nodes if d["slug"] == "org/empty"]
    assert done and all(d["commit_sha"] == "deadbeef" and d["job_id"] == "jc" for d in done)
    assert empty and all(d["commit_sha"] is None and d["job_id"] is None for d in empty)


def test_backfill_is_idempotent() -> None:
    """The second pass changes nothing — the ``$exists`` gate skips stamped rows."""
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = _artifacts({"org/done": 4})
    backfill = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts)

    first = backfill.run()
    second = backfill.run()

    assert first["total_modified"] > 0
    assert second["total_modified"] == 0


# ── the misattribution fix ───────────────────────────────────────────────────


def test_isolated_slug_legacy_rows_get_sentinel_not_the_live_commit() -> None:
    """A slug that already has SOME rows carrying a real ``commit_sha`` proves
    its remaining field-absent rows are from an EARLIER generation — they
    cannot have come from the job that produced the already-stamped rows.
    Attributing them to that job's commit (the pre-fix behaviour) is a
    factually wrong write, and it would also make the misattributed rows
    permanently indistinguishable from the live generation the moment a
    future index resolves that SAME commit again. They must get the reap
    sentinel instead — never the live commit.
    """
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "newcommit",
         "phase_started_at": "2026-05-01"},
    ])
    nodes = _FakeCollection([
        {"slug": "org/done", "node_id": "fresh", "commit_sha": "newcommit", "job_id": "jc"},
        {"slug": "org/done", "node_id": "legacy"},  # pre-isolation, no field
    ])
    artifacts = {name: _FakeCollection() for name in ARTIFACT_COLLECTIONS}
    artifacts["wiki_graph_nodes"] = nodes

    report = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    fresh = next(d for d in nodes.docs if d["node_id"] == "fresh")
    legacy = next(d for d in nodes.docs if d["node_id"] == "legacy")
    assert fresh["commit_sha"] == "newcommit"  # untouched — already had a value
    assert legacy["commit_sha"] == LEGACY_ARTIFACT_SENTINEL  # NOT "newcommit"
    assert legacy["job_id"] is None
    assert report["slugs"]["org/done"] == {
        "commit_sha": LEGACY_ARTIFACT_SENTINEL,
        "job_id": None,
        "strategy": "isolation_sentinel",
    }


def test_isolation_check_spans_every_artifact_collection() -> None:
    """The isolation check is per-SLUG across every artifact family, not
    per-collection: a slug whose NODES are isolated but whose ENTITIES are
    still entirely field-absent must still sentinel-stamp the entity rows —
    they are just as provably legacy, since the store always stamps both
    fields together on a fresh write.
    """
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/mixed", "status": "complete", "commit_sha": "newcommit",
         "phase_started_at": "2026-05-01"},
    ])
    nodes = _FakeCollection([
        {"slug": "org/mixed", "node_id": "n1", "commit_sha": "newcommit", "job_id": "jc"},
    ])
    entities = _FakeCollection([
        {"slug": "org/mixed", "id": "e1"},  # field-absent; no isolated entity row exists at all
    ])
    artifacts = {name: _FakeCollection() for name in ARTIFACT_COLLECTIONS}
    artifacts["wiki_graph_nodes"] = nodes
    artifacts["wiki_entities"] = entities

    ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    stamped_entity = next(d for d in entities.docs if d["id"] == "e1")
    assert stamped_entity["commit_sha"] == LEGACY_ARTIFACT_SENTINEL
    assert stamped_entity["job_id"] is None


def test_no_isolated_rows_falls_back_to_job_guess_unchanged() -> None:
    """A slug with NO isolated rows anywhere (the common case — e.g. every
    orphan slug and every never-attributed live project measured in
    production) is unaffected by the fix: the latest-job guess is still the
    best available signal and stays exactly as before.
    """
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/first-gen", "status": "complete", "commit_sha": "onlycommit",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = _artifacts({"org/first-gen": 5})
    report = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run()

    assert report["slugs"]["org/first-gen"] == {
        "commit_sha": "onlycommit",
        "job_id": "jc",
        "strategy": "job_guess",
    }


def test_sentinel_cannot_collide_with_a_real_git_sha() -> None:
    """Git commit shas are lowercase hex (7-40 chars); the sentinel must not be."""
    assert not re.fullmatch(r"[0-9a-f]{7,40}", LEGACY_ARTIFACT_SENTINEL)


def test_sentinel_is_dropped_by_mongos_real_nin_predicate() -> None:
    """The sentinel is reaped by a `$nin` of the shape supersede uses.

    Scope, stated precisely because it is narrower than it may read: this
    checks the PREDICATE FAMILY against a hand-written filter, not the store.
    It imports nothing from ``store.py`` and would still pass if
    ``supersede_graph_artifacts``' own filter were changed or broken, so it is
    NOT a regression test on production code — the real backstop for that is
    ``test_artifact_isolation.py``, which drives ``MongoWikiStore`` directly.

    What it does buy: confirmation that a sentinel-stamped row is dropped
    whether the next index lands on a NEW commit or on the SAME commit again.
    The same-commit case is the one the earlier latest-job attribution could
    not guarantee, and it is the reason the sentinel exists at all.
    """
    import mongomock

    col = mongomock.MongoClient()["scratch"]["wiki_graph_nodes"]
    col.insert_many([
        {"node_id": "legacy", "slug": "acme/x",
         "commit_sha": LEGACY_ARTIFACT_SENTINEL, "job_id": None},
        {"node_id": "live", "slug": "acme/x", "commit_sha": "newsha", "job_id": "jnew"},
        {"node_id": "unattributable", "slug": "acme/x", "commit_sha": None, "job_id": None},
    ])
    # A NEW commit supersedes.
    col.delete_many({"slug": "acme/x", "commit_sha": {"$nin": [None, "newsha"]}})
    remaining = {d["node_id"] for d in col.find({"slug": "acme/x"})}
    assert remaining == {"live", "unattributable"}  # sentinel reaped; None preserved

    # The SAME commit reindexes again — the scenario the old attribution
    # could not survive (job-guessed rows would then share ``keep_commit_sha``
    # with the live rows and never be reaped).
    col.insert_many([
        {"node_id": "legacy2", "slug": "acme/y",
         "commit_sha": LEGACY_ARTIFACT_SENTINEL, "job_id": None},
        {"node_id": "live2", "slug": "acme/y", "commit_sha": "samesha", "job_id": "j2"},
    ])
    col.delete_many({"slug": "acme/y", "commit_sha": {"$nin": [None, "samesha"]}})
    remaining2 = {d["node_id"] for d in col.find({"slug": "acme/y"})}
    assert remaining2 == {"live2"}  # sentinel reaped even on a same-commit reindex


def test_sentinel_dropped_by_json_drivers_retain_predicate() -> None:
    """The sentinel fails both arms of a keep-predicate shaped like the JSON one.

    Same scope caveat as the Mongo case above: the predicate is written out
    here rather than imported, so this cannot catch a change to
    ``_retain_jsonl`` itself. It answers only "does a sentinel-stamped row
    survive a keep-or-null test", and the answer is no for any real
    ``keep_commit_sha`` — matching the Mongo outcome, which is what makes the
    sentinel behave identically on both drivers.
    """
    def _kept(commit_sha: str | None, keep_commit_sha: str) -> bool:
        return commit_sha is None or commit_sha == keep_commit_sha

    assert _kept(None, "newsha") is True  # unattributable — preserved
    assert _kept("newsha", "newsha") is True  # live generation — preserved
    assert _kept(LEGACY_ARTIFACT_SENTINEL, "newsha") is False  # sentinel — dropped
    assert _kept(LEGACY_ARTIFACT_SENTINEL, "samesha") is False  # dropped on any commit


# ── dry-run ───────────────────────────────────────────────────────────────────


def test_dry_run_reports_counts_without_writing() -> None:
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = _artifacts({"org/done": 4})
    backfill = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts)

    dry = backfill.run(dry_run=True)
    assert dry["total_modified"] == 4 * len(ARTIFACT_COLLECTIONS)
    assert dry["slugs"]["org/done"]["strategy"] == "job_guess"
    # Nothing was actually written.
    for col in artifacts.values():
        assert all("commit_sha" not in d for d in col.docs)

    # A real run afterward still has the full amount of work to do.
    real = backfill.run()
    assert real["total_modified"] == dry["total_modified"]
    second = backfill.run()
    assert second["total_modified"] == 0


def test_dry_run_counts_server_side_and_never_materialises_documents() -> None:
    """The dry run must COUNT, not fetch — asserted as a call, not a number.

    A test that only checks the reported total is right passes just as happily
    against ``len(list(col.find(filt)))``, which is what this path used to do.
    That is not a cosmetic difference: the rows this migration targets include
    embedding vectors of tens of kilobytes each, so length-checking a cursor
    pulls gigabytes into the client to learn a number the server already knows
    — and it does it on ``--dry-run``, so an operator following the documented
    cautious order is the one who pays, while the real write they were being
    careful about would have completed. The observable property is therefore
    WHICH call is made, and only a recording collection can see it.
    """
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = {
        name: _RecordingCollection(col.docs)
        for name, col in _artifacts({"org/done": 4}).items()
    }

    report = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts).run(dry_run=True)

    # A non-zero count, so a fake that silently answered 0 could not pass.
    assert report["total_modified"] == 4 * len(ARTIFACT_COLLECTIONS)
    for name, col in artifacts.items():
        assert "count_documents" in col.calls, f"{name}: dry run never counted"
        assert "find" not in col.calls, f"{name}: dry run materialised documents"
        assert "update_many" not in col.calls, f"{name}: dry run wrote"


def test_dry_run_is_repeatable() -> None:
    """A dry run never mutates, so running it twice reports the same counts —
    unlike a real run, which is idempotent by becoming a no-op the second time.
    """
    jobs = _FakeCollection([
        {"job_id": "jc", "slug": "org/done", "status": "complete", "commit_sha": "deadbeef",
         "phase_started_at": "2026-05-01"},
    ])
    artifacts = _artifacts({"org/done": 3})
    backfill = ArtifactIsolationBackfill(jobs=jobs, artifacts=artifacts)

    first = backfill.run(dry_run=True)
    second = backfill.run(dry_run=True)
    assert first["total_modified"] == second["total_modified"] > 0
