"""Backfill artifact attribution onto pre-isolation wiki rows.

Graph nodes/edges/embeddings, entities/entity-edges/entity-embeddings and pages
gained ``commit_sha``/``job_id`` so a completed re-index can supersede the prior
commit's artifacts instead of unioning into them. Rows written before that had
neither field. This class stamps the existing rows so the new machinery has
something to reason about.

Design (the runnable Mongo CLI is ``scripts/backfill_wiki_artifact_isolation.py``):

- **Atomic + DI'd.** The collections it reads/writes are injected (duck-typed
  ``distinct``/``find``/``update_many``), so this stays a pure-Python engine — no
  ``pymongo`` import, no layering violation — and a test drives it against an
  in-memory fake with no live Mongo.
- **Idempotent by construction.** Every write is gated on
  ``commit_sha {$exists: false}``. After the first pass every row carries the
  field (a real value or ``None``), so a second pass matches nothing. Rows the
  store writes post-isolation always carry it too, so the backfill never touches
  a correctly-attributed row.
- **Non-destructive.** It only ``$set``s two fields; it never deletes. Reaping the
  union is the NEXT index's job (``supersede_graph_artifacts``), not the
  migration's — so a pass cannot lose data even if its attribution is imperfect.
- **``--dry-run`` previews without writing.** ``run(dry_run=True)`` counts what
  WOULD be stamped, using the same filter as the real write but issuing no
  ``update_many`` — this tool mutates a live database, and a preview is the only
  way to see the blast radius before running it for real. The count is a
  server-side ``count_documents``, NEVER a materialised ``find``: the rows this
  migration targets include embedding vectors of tens of kilobytes each, so
  fetching them merely to length-check a cursor costs gigabytes of client memory
  to answer a question the server answers for free — and it would do so on the
  ``--dry-run`` path, making the cautious option the one that falls over while
  the real write it was meant to de-risk succeeds.

Attribution policy (see :meth:`attribution_for` and :meth:`_has_isolated_rows`):
per-row commit provenance was never recorded and cannot be reconstructed, so
attribution is decided at whole-slug granularity — but the decision branches on
a fact read directly off the artifact collections, not inferred from job history:
does this slug already have ANY row, in ANY artifact family, carrying a real
``commit_sha``?

- **No isolated rows anywhere (a genuinely first generation for this slug).**
  The slug's latest job that resolved a commit is the best available signal —
  that commit's artifacts are the live ones under merge-on-upsert
  semantics, so the field-absent stragglers are conservatively attributed to
  it. A slug with no job that ever resolved a commit (a catalog workspace, or a
  clone that never succeeded) is genuinely un-attributable: its rows are
  stamped ``None``/``None``, which supersede then PRESERVES — the honest
  outcome for a row no index owns.
- **Isolated rows already exist in ANY family.** The slug's remaining
  field-absent rows cannot have come from the job that produced those
  already-stamped rows — they predate it. Attributing them to that job's
  commit would be a factually wrong write, and a dangerous one: it would make
  the misattributed rows PERMANENTLY indistinguishable from the live
  generation the moment a future index resolves that SAME commit again (a
  repeat build, or a refresh with no upstream change — this recurs in
  practice, not a hypothetical), since the preserve rule in
  ``supersede_graph_artifacts`` keeps every row whose ``commit_sha`` matches
  ``keep_commit_sha`` regardless of how it got that value. These rows are
  instead stamped with :data:`LEGACY_ARTIFACT_SENTINEL` — a value that is not
  a valid commit sha, so it can never equal a future ``keep_commit_sha`` and is
  therefore excluded by ``supersede``'s ``$nin: [None, keep_commit_sha]``
  filter on the NEXT index, whatever commit that index resolves to.

That reaping route exists for every artifact family EXCEPT ``wiki_pages``, which
``supersede_graph_artifacts`` does not touch at all: a page is replaced in place
by ``save_page``'s ``(slug, page_id)`` upsert and removed by ``prune_pages``
against the finalize plan, never by commit. So a sentinel on a page is pure
PROVENANCE — it reads as "written before attribution existed, owning commit
unknown" and stays until the next index rewrites or prunes that page. Nothing
compares it, and nothing reaps it; do not describe it as a reap marker there.
"""
from __future__ import annotations

from typing import Any, Protocol

#: Stamped onto a slug's field-absent rows when the slug ALREADY has isolated
#: (real-commit) rows in some artifact family — see the module docstring's
#: "Isolated rows already exist" case. Deliberately not valid hex, so it can
#: never collide with a real git commit sha and therefore never survives a
#: future ``supersede_graph_artifacts`` call as a false "keep".
LEGACY_ARTIFACT_SENTINEL: str = "legacy-pre-isolation"


class _Collection(Protocol):
    """The tiny slice of a Mongo collection this backfill uses (duck-typed)."""

    def distinct(self, field: str, filt: dict[str, Any] | None = None) -> list[Any]: ...
    def find(self, filt: dict[str, Any]) -> Any: ...
    def count_documents(self, filt: dict[str, Any]) -> int: ...
    def update_many(self, filt: dict[str, Any], update: dict[str, Any]) -> Any: ...


# The artifact collections that gained ``commit_sha``/``job_id``. ``wiki_pages``
# belongs here even though no commit-keyed reaper ever reads its stamp (see the
# module docstring's pages carve-out): the live indexer stamps every page it
# writes, so omitting the collection would leave pages the ONE family whose
# column is populated on new writes but absent on unstamped rows — and
# ``_has_isolated_rows`` reads the column across every family, so that gap would
# be a hole in the isolation check itself, not merely a cosmetic inconsistency.
ARTIFACT_COLLECTIONS: tuple[str, ...] = (
    "wiki_graph_nodes",
    "wiki_graph_edges",
    "wiki_embeddings",
    "wiki_entities",
    "wiki_entity_edges",
    "wiki_entity_embeddings",
    "wiki_pages",
)


class ArtifactIsolationBackfill:
    """Stamp pre-isolation artifact rows with their owning commit + job.

    ``jobs`` is the ``wiki_jobs`` collection; ``artifacts`` maps each artifact
    collection name (see :data:`ARTIFACT_COLLECTIONS`) to its collection.
    """

    def __init__(
        self, *, jobs: _Collection, artifacts: dict[str, _Collection]
    ) -> None:
        """Inject the jobs collection + the artifact collections by name."""
        self._jobs = jobs
        self._artifacts = artifacts

    @staticmethod
    def attribution_for(jobs: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        """Return ``(commit_sha, job_id)`` to stamp a slug's unstamped rows with.

        Only consulted for a slug with NO isolated rows yet (see
        :meth:`_has_isolated_rows`) — for a slug that already has some, the
        caller uses :data:`LEGACY_ARTIFACT_SENTINEL` instead and never reaches
        this method.

        The slug's latest index that resolved a commit — a ``complete`` one when
        any exists, else the latest attempt that at least got past clone. Ordered
        by ``phase_started_at`` (ISO-8601, so lexicographic order is chronological)
        with ``job_id`` as a stable tiebreak. ``(None, None)`` when no job ever
        resolved a commit: the slug is genuinely un-attributable and its rows stay
        commit-less, which supersede preserves.
        """
        with_commit = [j for j in jobs if j.get("commit_sha")]
        if not with_commit:
            return (None, None)
        complete = [j for j in with_commit if j.get("status") == "complete"]
        pool = complete or with_commit
        latest = max(
            pool,
            key=lambda j: (j.get("phase_started_at") or "", str(j.get("job_id") or "")),
        )
        return (latest.get("commit_sha"), latest.get("job_id"))

    def _has_isolated_rows(self, slug: str) -> bool:
        """Return True if *slug* already carries a real ``commit_sha`` anywhere.

        Checked across EVERY artifact family, not just the one about to be
        stamped: once any family for this slug has moved to per-commit
        attribution, a field-absent row in any OTHER family is just as
        provably a straggler — the store always stamps both fields
        together on a fresh write (see the module docstring's idempotency
        note), so a slug with isolated rows anywhere never legitimately
        produces a field-absent row anywhere else.
        """
        for col in self._artifacts.values():
            if any(col.distinct("commit_sha", {"slug": slug})):
                return True
        return False

    def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Stamp every un-attributed artifact row; return a summary report.

        Idempotent: the ``$exists: false`` gate means a re-run stamps nothing.
        The report carries per-collection modified counts and the per-slug
        attribution decision (with a ``strategy`` naming which case applied —
        ``job_guess``, ``isolation_sentinel``, or ``unattributable``), so an
        operator can see exactly what was inferred.

        ``dry_run=True`` counts what the same filter WOULD match and issues no
        ``update_many`` — the counts in the returned report are then a preview,
        not a record of a write that happened. It counts server-side and pulls
        no documents back, so previewing a migration is never more expensive
        than performing it.
        """
        slugs: set[str] = set()
        for col in self._artifacts.values():
            slugs.update(str(s) for s in col.distinct("slug"))

        collections: dict[str, int] = {name: 0 for name in self._artifacts}
        per_slug: dict[str, dict[str, Any]] = {}

        for slug in sorted(slugs):
            if self._has_isolated_rows(slug):
                commit_sha: str | None = LEGACY_ARTIFACT_SENTINEL
                job_id: str | None = None
                strategy = "isolation_sentinel"
            else:
                jobs = list(self._jobs.find({"slug": slug}))
                commit_sha, job_id = self.attribution_for(jobs)
                strategy = "job_guess" if commit_sha is not None else "unattributable"

            set_doc = {"commit_sha": commit_sha, "job_id": job_id}
            for name, col in self._artifacts.items():
                filt = {"slug": slug, "commit_sha": {"$exists": False}}
                if dry_run:
                    # ``count_documents``, never ``len(list(find(...)))``: the
                    # targeted rows include embedding vectors tens of kilobytes
                    # each, so materialising them to length-check a cursor makes
                    # the preview cost orders of magnitude more than the write
                    # it is previewing. The server counts; nothing is fetched.
                    n = col.count_documents(filt)
                else:
                    res = col.update_many(filt, {"$set": set_doc})
                    n = int(getattr(res, "modified_count", 0) or 0)
                collections[name] += n
            per_slug[slug] = {
                "commit_sha": commit_sha,
                "job_id": job_id,
                "strategy": strategy,
            }

        return {
            "collections": collections,
            "slugs": per_slug,
            "total_modified": sum(collections.values()),
        }


__all__ = ["ARTIFACT_COLLECTIONS", "LEGACY_ARTIFACT_SENTINEL", "ArtifactIsolationBackfill"]
