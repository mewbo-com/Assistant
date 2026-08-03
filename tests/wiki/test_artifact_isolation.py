"""Per-job/commit artifact isolation — attribution, supersede, commit-scoped skip.

Before this, ``upsert_nodes`` merged with no delete-by-slug, so the store was the
UNION of every commit ever indexed for a slug: files deleted months ago were
still served, and a node count could not mean "the graph for THIS commit is
built". Every test here pins one half of the fix — artifacts carry their commit,
a completed re-index reaps the prior commit's, the skip predicate counts only the
job's own commit, and commit-less (QA-minted) rows are preserved.

The supersede/reap-predicate tests run against BOTH store drivers (``store``
fixture below — real ``JsonWikiStore`` / ``MongoWikiStore`` over ``mongomock``),
because the two implement the same contract by different mechanisms: Mongo
filters with ``{"commit_sha": {"$nin": [None, keep]}}`` (a query operator that
also treats a field ENTIRELY ABSENT from the document as matching ``None``),
JSON filters in Python with ``it.commit_sha is None or it.commit_sha == keep``
after every row has already passed through the Pydantic model (whose default
turns an absent field into ``None`` at load time). Only ``tmp_path``/mongomock
are stubbed; the real store code runs.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import mongomock
import pytest
from mewbo_graph.entities.types import Entity, EntityEmbedding, EntityRelation
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore, WikiStoreBase
from mewbo_graph.wiki.types import (
    CommitScope,
    Embedding,
    Frontmatter,
    GraphEdge,
    IndexingJob,
    WikiPage,
    make_graph_node,
)

_A = "a" * 40
_B = "b" * 40


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path):
    """Both store drivers behind the same ``WikiStoreBase`` contract.

    Mirrors the idiom in ``test_store_memory.py``: mongomock stands in for a
    real MongoDB server so no external service is required.
    """
    if request.param == "json":
        return JsonWikiStore(root_dir=tmp_path / "wiki")
    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_artifacts")


def _node(slug: str, nid: str, *, file: str = "a.py"):
    return make_graph_node(
        slug=slug, node_id=nid, type="Function", name=nid, file=file, range=(0, 1)
    )


def _job(store: WikiStoreBase, *, job_id: str, slug: str, commit: str | None, status: str):
    job = IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None, commitSha=commit,
    )
    store.create_job(job)
    return job


# ── attribution + supersede at the store seam ────────────────────────────────


def test_two_commits_do_not_union_after_supersede(store) -> None:
    """A completed re-index reaps the prior commit's nodes instead of unioning."""
    slug = "org/repo"
    # Commit A built three nodes; commit B rebuilt the two shared ones (n3's file
    # was deleted, so B never re-emits it). The shared ids overwrite; n3 lingers.
    store.upsert_nodes(
        slug, [_node(slug, "n1"), _node(slug, "n2"), _node(slug, "n3")], commit_sha=_A
    )
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_B)
    # ``every()`` throughout on purpose: the claim is that supersede physically
    # REAPS the prior generation, so every read here must be the union — a
    # commit-scoped read would filter n3 out and pass even if nothing was reaped.
    every = CommitScope.every()
    assert {n.node_id for n in store.query_graph(slug, scope=every)} == {
        "n1", "n2", "n3",
    }  # the union

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    survivors = {n.node_id for n in store.query_graph(slug, scope=every)}
    assert survivors == {"n1", "n2"}  # n3 (commit A only) is gone — no union
    assert all(n.commit_sha == _B for n in store.query_graph(slug, scope=every))


def test_file_deleted_between_commits_stops_being_served(store) -> None:
    """A node whose file was deleted at the new commit is not returned to retrieval.

    ``query_graph`` is exactly what ``HybridRetriever._graph_candidates`` reads, so
    proving it stops returning the stale node proves retrieval stops serving it.
    """
    slug = "org/repo"
    store.upsert_nodes(
        slug, [_node(slug, "gone", file="deleted.py"), _node(slug, "kept", file="kept.py")],
        commit_sha=_A,
    )
    # Commit B: deleted.py is gone, so only kept.py's node is re-emitted.
    store.upsert_nodes(slug, [_node(slug, "kept", file="kept.py")], commit_sha=_B)

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    # ``every()``: proving the stale node is UNREACHABLE, not merely out of scope.
    files = {n.file for n in store.query_graph(slug, scope=CommitScope.every())}
    assert files == {"kept.py"}
    assert "deleted.py" not in files


def test_supersede_preserves_commitless_entities(store) -> None:
    """QA-minted (commit-less) entities survive a re-index; prior-commit ones don't.

    The mint tool also runs in a Q&A session, which carries no job — those
    entities are accretive memory, not a per-commit snapshot, so supersede must
    leave every ``None``-stamped row untouched while reaping other commits'.
    """
    slug = "org/repo"
    store.upsert_entities(slug, [Entity(name="OldConcept", type="concept")], commit_sha=_A)
    store.upsert_entities(slug, [Entity(name="CurrentConcept", type="concept")], commit_sha=_B)
    store.upsert_entities(slug, [Entity(name="QaConcept", type="concept")])  # commit-less

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    names = {e.name for e in store.query_entities(slug)}
    assert names == {"CurrentConcept", "QaConcept"}  # OldConcept reaped; QA preserved


def test_supersede_is_idempotent(store) -> None:
    """A second supersede for the same commit finds nothing left to reap."""
    slug = "org/repo"
    store.upsert_nodes(slug, [_node(slug, "n1")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "n2")], commit_sha=_B)

    first = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)
    second = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    assert first["nodes"] == 1
    assert second["nodes"] == 0


# ── supersede reap predicate — every collection, every commit_sha shape ──────
#
# Live production data is 100% field-absent: nothing ever wrote an explicit
# ``commit_sha: null``, rows either carry a real commit or predate the field
# entirely. That is the shape that actually matters, and it is NOT the same
# document shape a Pydantic-model write can produce — ``model_dump``/
# ``model_dump_json`` always serialize a declared field, at worst as ``null``.
# ``_write_field_absent`` below reaches past the model on purpose to reproduce
# it: a raw dict/JSONL line with the ``commit_sha`` key removed entirely.

_MONGO_COLLECTION: dict[str, str] = {
    "nodes": "wiki_graph_nodes",
    "edges": "wiki_graph_edges",
    "embeddings": "wiki_embeddings",
    "entities": "wiki_entities",
    "entity_edges": "wiki_entity_edges",
    "entity_embeddings": "wiki_entity_embeddings",
}

# (slug, suffix) -> a freshly built, UNSTAMPED model instance for that family.
# One row per family is enough to prove the reap predicate; the id embeds
# *suffix* so four rows built for the same family never collide on their
# dedup key (node_id / (source,target,type) / entity id / ...).
_KIND_BUILDERS: dict[str, Callable[[str, str], Any]] = {
    "nodes": lambda slug, suffix: _node(slug, f"node-{suffix}"),
    "edges": lambda slug, suffix: GraphEdge(
        slug=slug, source=f"src-{suffix}", target=f"tgt-{suffix}", type="CALLS"
    ),
    "embeddings": lambda slug, suffix: Embedding(
        slug=slug, node_id=f"emb-{suffix}", vector=[1.0, 0.0], model="test-model", dim=2
    ),
    "entities": lambda slug, suffix: Entity(name=f"Entity-{suffix}", type="concept"),
    "entity_edges": lambda slug, suffix: EntityRelation(
        source_id=f"esrc-{suffix}", target_id=f"etgt-{suffix}", type="relates_to"
    ),
    "entity_embeddings": lambda slug, suffix: EntityEmbedding(
        slug=slug, entity_id=f"eemb-{suffix}", vector=[1.0, 0.0], model="test-model", dim=2
    ),
}

# The natural identity a survivor is recognizable by, read off a row returned
# by the family's own public query method — never an index into a list.
_KIND_ROW_ID: dict[str, Callable[[Any], str]] = {
    "nodes": lambda row: row.node_id,
    "edges": lambda row: row.source,
    "embeddings": lambda row: row.node_id,
    "entities": lambda row: row.id,
    "entity_edges": lambda row: row.id,
    "entity_embeddings": lambda row: row.entity_id,
}

# The PUBLIC read for each family — no reaching past the model on the read
# side. Embeddings have no "list all" method, only vector search, so a
# generous k over a small fixed-direction vector recovers the whole pool.
# The two graph reads take ``CommitScope.every()`` for the same reason the
# other four families have no scope at all: this asserts which rows the reap
# DELETED, so a scoped read would hide a survivor behind a filter instead.
_KIND_SURVIVORS: dict[str, Callable[[Any, str], set[str]]] = {
    "nodes": lambda store, slug: {
        n.node_id for n in store.query_graph(slug, scope=CommitScope.every())
    },
    "edges": lambda store, slug: {
        e.source for e in store.list_edges(slug, scope=CommitScope.every())
    },
    "embeddings": lambda store, slug: {
        e.node_id for e in store.vector_search(slug, [1.0, 0.0], k=100)
    },
    "entities": lambda store, slug: {e.id for e in store.query_entities(slug)},
    "entity_edges": lambda store, slug: {e.id for e in store.list_entity_edges(slug)},
    "entity_embeddings": lambda store, slug: {
        e.entity_id for e in store.entity_vector_search(slug, [1.0, 0.0], k=100)
    },
}


def _write_field_absent(store: WikiStoreBase, kind: str, slug: str, row: Any) -> None:
    """Persist *row* with its ``commit_sha`` key OMITTED, not just null-valued.

    Must run AFTER every other write to this (store, kind, slug) in a test: the
    JSON driver's ``upsert_*`` always rewrites the WHOLE jsonl file (read every
    row back through the model, dump every row back out through the model), so
    an earlier bare line would be silently re-serialized with an explicit
    ``commit_sha: null`` by a later upsert — indistinguishable from the
    explicit-None case this is supposed to be different from. Appending last
    sidesteps that; the raw line is never round-tripped before supersede reads
    it.
    """
    doc = row.model_dump(mode="json", by_alias=False)
    doc.pop("commit_sha", None)
    if isinstance(store, JsonWikiStore):
        path: Path = getattr(store, f"_{kind}_path")(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(doc) + "\n")
    else:
        # Graph-family models (nodes/edges/embeddings/entity_embeddings) carry
        # their own ``slug`` field; entities/entity_edges don't — Mongo stamps
        # it onto the document separately (mirrors ``upsert_entities`` /
        # ``upsert_entity_edges`` in store.py), and the reap query filters on
        # that document-level key regardless of which family it came from.
        doc.setdefault("slug", slug)
        store._col(_MONGO_COLLECTION[kind]).insert_one(doc)


@pytest.mark.parametrize("kind", sorted(_KIND_BUILDERS))
def test_supersede_reap_predicate_per_family(store, kind: str) -> None:
    """The load-bearing predicate, pinned per family on both drivers.

    Four rows: a field-ABSENT one (the shape 100% of production data is in), an
    explicit-``None`` one, one stamped with the commit being kept, and one
    stamped with a real, different commit. Only the last is stale. Asserted by
    SET MEMBERSHIP of the survivors' own ids, not just a count — a count-only
    assertion would pass just as well if the wrong row were the one reaped.
    """
    slug = "org/repo"
    upsert = getattr(store, f"upsert_{kind}")
    build = _KIND_BUILDERS[kind]
    row_id = _KIND_ROW_ID[kind]

    none_row = build(slug, "none")
    upsert(slug, [none_row])  # no commit_sha kwarg -> stamped explicit None

    keep_row = build(slug, "keep")
    upsert(slug, [keep_row], commit_sha=_B)

    stale_row = build(slug, "stale")
    upsert(slug, [stale_row], commit_sha=_A)

    absent_row = build(slug, "absent")
    _write_field_absent(store, kind, slug, absent_row)  # LAST — see its docstring

    counts = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)
    assert counts[kind] == 1  # exactly the real-other-commit row

    survivors = _KIND_SURVIVORS[kind](store, slug)
    assert survivors == {row_id(none_row), row_id(keep_row), row_id(absent_row)}
    assert row_id(stale_row) not in survivors

    # Idempotent: replaying with the same keep commit reaps nothing further
    # and disturbs no survivor.
    second = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)
    assert second[kind] == 0
    assert _KIND_SURVIVORS[kind](store, slug) == survivors


def test_supersede_returns_exact_per_collection_counts(store) -> None:
    """The returned dict is six real per-collection counts, not a rollup total.

    One stale + one keep row in EVERY family, reaped in a single call, so a
    collection silently skipped (or two collections' counts crossed) shows up
    as a wrong entry instead of an accidentally-correct sum.
    """
    slug = "org/repo"
    for kind, build in _KIND_BUILDERS.items():
        upsert = getattr(store, f"upsert_{kind}")
        upsert(slug, [build(slug, "keep")], commit_sha=_B)
        upsert(slug, [build(slug, "stale")], commit_sha=_A)

    counts = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    assert counts == {kind: 1 for kind in _KIND_BUILDERS}


# ── commit-scoped skip predicate ─────────────────────────────────────────────


def test_skip_predicate_answers_per_commit_not_per_union(store) -> None:
    """"graph for THIS commit built" — not "some commit built a graph for the slug"."""
    slug = "org/repo"
    # The graph in the store belongs to commit A.
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_A)
    store.upsert_entities(slug, [Entity(name="Widget", type="concept")], commit_sha=_A)

    # A resume of the job that BUILT commit A skips graph + enrich.
    built = ResumePlan.build(
        store, _job(store, job_id="jA", slug=slug, commit=_A, status="interrupted")
    )
    assert "graph" in built.skip
    assert "enrich" in built.skip

    # A resume of a DIFFERENT commit sees no graph for itself — the union of A's
    # nodes must not read as "commit B's graph is built".
    not_built = ResumePlan.build(
        store, _job(store, job_id="jB", slug=slug, commit=_B, status="interrupted")
    )
    assert "graph" not in not_built.skip
    assert "enrich" not in not_built.skip


def test_count_graph_nodes_matches_only_its_commit(store) -> None:
    """The store count that backs the predicate is exact, not a union count."""
    slug = "org/repo"
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "n3")], commit_sha=_B)

    assert store.count_graph_nodes(slug, commit_sha=_A) == 2
    assert store.count_graph_nodes(slug, commit_sha=_B) == 1
    assert store.count_graph_nodes(slug, commit_sha="c" * 40) == 0
    # the union is still 3 until supersede
    assert len(store.query_graph(slug, scope=CommitScope.every())) == 3


# ── finalize drives the supersede end-to-end ─────────────────────────────────
#
# JSON-only: this exercises the full WikiFinalizeTool (job submission, plan,
# page save, platform-description fetch stub) rather than just the store seam,
# and Mongo parity for that whole surface is outside this file's scope — the
# store-level tests above already pin the reap predicate itself on both
# drivers.


def _page(pid: str) -> WikiPage:
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid),
        body="x", toc=[], nav=[],
    )


def test_finalize_supersedes_prior_commit_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``wiki_finalize`` reaps prior-commit artifacts once it completes.

    Wires the whole seam: a job at commit B finalizes over a store still holding
    commit A's straggler node, and the completed index leaves only B's graph.
    """
    import mewbo_graph.plugins.wiki.finalize as finalize_mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    slug = "org/repo"

    _job(store, job_id="jobB", slug=slug, commit=_B, status="finalizing")
    store.attach_job_session("jobB", "sessB")
    store.save_job_submission("jobB", {
        "repoUrl": f"https://git.example.com/{slug}", "slug": slug,
        "platform": "gitea", "language": "Python", "filterMode": "exclude",
        "dirs": [], "files": [],
    })
    # The store holds commit A's straggler AND commit B's fresh node.
    store.upsert_nodes(slug, [_node(slug, "straggler")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "current")], commit_sha=_B)
    store.save_job_plan("jobB", [{"id": "overview", "title": "Overview"}])
    store.save_page(slug, _page("overview"), commit_sha=_B, job_id="jobB")

    tool = WikiFinalizeTool(session_id="sessB")
    runtime = SimpleNamespace(wiki_store=store)
    step = SimpleNamespace(tool_input={"landingPageId": "overview"})
    # Stub the platform description fetch so finalize never touches the network.
    with patch.object(finalize_mod, "_resolve_runtime", return_value=runtime), \
         patch.object(finalize_mod, "_fetch_description", return_value=""):
        result = asyncio.run(tool.handle(step))

    assert "error" not in result.content
    assert store.get_job("jobB").status == "complete"
    # ``every()``: the straggler must be GONE from the store, not merely
    # filtered out of commit B's generation — that is what finalize's supersede
    # is claimed to have done.
    survivors = {n.node_id for n in store.query_graph(slug, scope=CommitScope.every())}
    assert survivors == {"current"}  # the commit-A straggler was superseded
