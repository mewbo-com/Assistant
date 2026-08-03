"""GraphDeltaIndexer — scoped retract + re-parse + reverse-dependency closure."""
from __future__ import annotations

import pytest
from loguru import logger
from mewbo_graph.wiki.embedder import EmbedderProtocol
from mewbo_graph.wiki.graph import GraphParseResult, _stable_id
from mewbo_graph.wiki.memory_types import FileManifest
from mewbo_graph.wiki.refresh import ChangeSet, GraphDeltaIndexer
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import CommitScope, Embedding, GraphEdge, make_graph_node

from .conftest import FakeEmbedder, FakeParser

SLUG = "org/repo"

# Every graph read here is generation-agnostic: the seed is commit-less and a
# delta only re-stamps the files it touched, so the store legitimately holds a
# MIX of stamps. ``every()`` is the union these assertions were written against
# — scoping to the delta's commit would hide the untouched files.
_EVERY = CommitScope.every()


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _node(nid, typ, name, f):
    return make_graph_node(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


def _seed_two_file_graph(store):
    """a.py defines verify(); b.py's caller() CALLS verify (cross-file edge)."""
    syn_verify = _stable_id(SLUG, "Function", "verify", "<external>", 0)
    store.upsert_nodes(
        SLUG,
        [
            _node("fileA", "File", "a.py", "a.py"),
            _node("nVerify", "Function", "verify", "a.py"),
            _node("fileB", "File", "b.py", "b.py"),
            _node("nCaller", "Function", "caller", "b.py"),
        ],
    )
    store.upsert_edges(
        SLUG,
        [
            GraphEdge(slug=SLUG, source="fileA", target="nVerify", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="fileB", target="nCaller", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="nCaller", target=syn_verify, type="CALLS"),
        ],
    )
    store.upsert_file_manifest(
        SLUG,
        [
            FileManifest(
                slug=SLUG, path="a.py", content_hash="hA",
                entity_keys=["a.py", "a.py#verify"],
            ),
            FileManifest(
                slug=SLUG, path="b.py", content_hash="hB",
                entity_keys=["b.py", "b.py#caller"],
            ),
        ],
    )


def test_modified_file_reparses_and_finds_reverse_dependents(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    # a.py re-parses to: keep verify, add newhelper
    reparse = GraphParseResult(
        nodes=[
            _node("fileA2", "File", "a.py", "a.py"),
            _node("nVerify2", "Function", "verify", "a.py"),
            _node("nHelper", "Function", "newhelper", "a.py"),
        ],
        edges=[
            GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="fileA2", target="nHelper", type="CONTAINS"),
        ],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(
        added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"}
    )
    delta = indexer.apply(SLUG, root, change, commit="c2")

    assert "a.py#newhelper" in delta.added_keys
    assert "a.py#verify" in delta.modified_keys
    # reverse-dependency closure: b.py#caller CALLS verify → impacted
    assert "b.py#caller" in delta.affected
    # graph mutated: stale node gone, new node present
    ids = {n.node_id for n in store.query_graph(SLUG, scope=_EVERY)}
    assert "nVerify" not in ids and "nHelper" in ids
    # manifest refreshed for a.py
    man = store.get_file_manifest(SLUG, "a.py")
    assert man.content_hash == "hA2"
    assert "a.py#newhelper" in man.entity_keys


def test_early_cutoff_when_reparse_identical(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    # identical re-parse (same entity_keys + same edges) → early cutoff
    reparse = GraphParseResult(
        nodes=[
            _node("fileA", "File", "a.py", "a.py"),
            _node("nVerify", "Function", "verify", "a.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA", target="nVerify", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "a.py" in delta.early_cutoff_files
    assert delta.affected == frozenset()  # zero downstream work
    # manifest hash still advances (file content changed even if graph didn't)
    assert store.get_file_manifest(SLUG, "a.py").content_hash == "hA2"


def test_deleted_file_retracts_and_flags_dependents(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    indexer = GraphDeltaIndexer(store, parser=FakeParser({}))
    change = ChangeSet(added=[], modified=[], deleted=["a.py"], current_hashes={})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "a.py#verify" in delta.removed_keys
    assert "b.py#caller" in delta.affected  # caller of removed verify
    # a.py nodes retracted; manifest entry gone
    files = {n.file for n in store.query_graph(SLUG, scope=_EVERY)}
    assert "a.py" not in files
    assert store.get_file_manifest(SLUG, "a.py") is None


def test_added_file_indexes_new_entities(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    reparse = GraphParseResult(
        nodes=[_node("fileC", "File", "c.py", "c.py"), _node("nNew", "Function", "fresh", "c.py")],
        edges=[GraphEdge(slug=SLUG, source="fileC", target="nNew", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"c.py": reparse}))
    change = ChangeSet(added=["c.py"], modified=[], deleted=[], current_hashes={"c.py": "hC"})
    delta = indexer.apply(SLUG, root, change, commit="c2")
    assert "c.py#fresh" in delta.added_keys
    assert store.get_file_manifest(SLUG, "c.py").content_hash == "hC"


def test_malformed_parse_result_is_rejected_before_upsert(store, tmp_path) -> None:
    """A bad parse result (duplicate node_id) must be rejected, not persisted.

    ``apply`` must not go straight from ``parse_file`` to
    ``upsert_nodes``/``upsert_edges`` with NO ``CodeGraph`` validation: gating
    only the first full index (``build_graph_core``) leaves a steady-state
    refresh bypassing schema-v2 enforcement entirely.
    """
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    dup_id = "dupNode"
    reparse = GraphParseResult(
        nodes=[
            _node("fileA2", "File", "a.py", "a.py"),
            _node(dup_id, "Function", "one", "a.py"),
            _node(dup_id, "Function", "two", "a.py"),  # SAME id — invalid
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA2", target=dup_id, type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(
        added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"}
    )
    with pytest.raises(ValueError, match="graph schema validation failed"):
        indexer.apply(SLUG, root, change, commit="c2")

    # Nothing from the bad reparse landed — the pre-existing a.py graph is
    # already retracted by step 2 (stale-node cleanup precedes validation),
    # so the store correctly ends up WITHOUT a.py rather than with corrupt data.
    ids = {n.node_id for n in store.query_graph(SLUG, scope=_EVERY)}
    assert dup_id not in ids


# ── embedding lifecycle ─────────────────────────────────────────────────────
#
# Two directions of ONE invariant, asserted as set differences so neither can
# be satisfied by the implementation merely doing what it happens to do today:
#   (a) no orphaned vectors    — every embedded node_id resolves to a live node
#   (b) no unvectorised nodes  — every live node carries a vector
# A retract that drops nodes without their vectors breaks (a); a re-parse that
# upserts nodes without embedding them breaks (b).


def _seed_vectors(store, node_ids: list[str], *, commit: str = "c1") -> None:
    store.upsert_embeddings(
        SLUG,
        [
            Embedding(slug=SLUG, node_id=nid, vector=[1.0, 0.0], model="seed", dim=2)
            for nid in node_ids
        ],
        commit_sha=commit,
    )


def _vectors(store) -> dict[str, Embedding]:
    return {e.node_id: e for e in store.vector_search(SLUG, [1.0, 0.0], k=100)}


def _live_ids(store) -> set[str]:
    return {n.node_id for n in store.query_graph(SLUG, scope=_EVERY)}


def _modified_a_py():
    """a.py re-parses to a fresh File node, a renamed verify, and a new helper."""
    return GraphParseResult(
        nodes=[
            _node("fileA2", "File", "a.py", "a.py"),
            _node("nVerify2", "Function", "verify", "a.py"),
            _node("nHelper", "Function", "newhelper", "a.py"),
        ],
        edges=[
            GraphEdge(slug=SLUG, source="fileA2", target="nVerify2", type="CONTAINS"),
            GraphEdge(slug=SLUG, source="fileA2", target="nHelper", type="CONTAINS"),
        ],
        skipped=[],
    )


def _modify_change():
    return ChangeSet(added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"})


def test_fake_embedder_satisfies_the_real_embedder_protocol() -> None:
    """The shared double is checked against the seam it stands in for.

    ``EmbedderProtocol`` is ``runtime_checkable``, so this pins the double to
    the production surface: a signature drift on ``Embedder`` that the double
    does not follow fails here instead of silently making every embedding test
    exercise a shape production no longer has.
    """
    assert isinstance(FakeEmbedder(), EmbedderProtocol)


def test_scoped_apply_leaves_no_orphaned_vectors_and_no_unvectorised_nodes(
    store, tmp_path
) -> None:
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"])
    indexer = GraphDeltaIndexer(
        store, parser=FakeParser({"a.py": _modified_a_py()}), embedder=FakeEmbedder()
    )

    indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")

    live, vectors = _live_ids(store), set(_vectors(store))
    assert vectors - live == set()  # (a) no vector points at a node that is gone
    assert live - vectors == set()  # (b) no node was re-indexed without a vector


def test_reparsed_nodes_are_embedded_from_their_own_embedding_text(
    store, tmp_path
) -> None:
    """The delta path embeds the SAME text the full index would.

    Both paths read ``GraphNode.embedding_text``, so a symbol re-embedded by a
    refresh lands in the same vector neighbourhood as one embedded by a full
    index — otherwise retrieval quality would depend on which path last touched
    a file.
    """
    _seed_two_file_graph(store)
    embedder = FakeEmbedder()
    reparse = _modified_a_py()
    indexer = GraphDeltaIndexer(
        store, parser=FakeParser({"a.py": reparse}), embedder=embedder
    )

    indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")

    assert embedder.calls, "the re-parsed scope was never handed to the embedder"
    embedded = dict(pair for call in embedder.calls for pair in call)
    assert embedded == {n.node_id: n.embedding_text for n in reparse.nodes}


def test_reparsed_vectors_carry_the_refreshed_commit(store, tmp_path) -> None:
    """New vectors are stamped with the refresh's commit, like its nodes.

    ``supersede_graph_artifacts`` reaps by ``commit_sha``, so a vector left
    stamped with a superseded commit is reaped out from under a live node the
    next time a full index completes.
    """
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"], commit="c1")
    indexer = GraphDeltaIndexer(
        store, parser=FakeParser({"a.py": _modified_a_py()}), embedder=FakeEmbedder()
    )

    indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")

    vectors = _vectors(store)
    assert {vectors[nid].commit_sha for nid in ("fileA2", "nVerify2", "nHelper")} == {"c2"}
    # An untouched file is outside the scope — its vectors keep their own stamp.
    assert vectors["nCaller"].commit_sha == "c1"


def test_early_cutoff_file_is_re_embedded_after_its_retract(store, tmp_path) -> None:
    """An identical re-parse still needs its vectors back.

    Early cutoff means "nothing downstream to do", not "nothing happened": the
    retract already deleted this file's nodes AND their vectors, so skipping the
    re-embed would leave the re-upserted nodes permanently unvectorised while
    the delta reported zero affected entities.
    """
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"])
    identical = GraphParseResult(
        nodes=[
            _node("fileA", "File", "a.py", "a.py"),
            _node("nVerify", "Function", "verify", "a.py"),
        ],
        edges=[GraphEdge(slug=SLUG, source="fileA", target="nVerify", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(
        store, parser=FakeParser({"a.py": identical}), embedder=FakeEmbedder()
    )

    delta = indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")

    assert "a.py" in delta.early_cutoff_files
    live, vectors = _live_ids(store), set(_vectors(store))
    assert live - vectors == set()
    assert vectors - live == set()


def test_deleted_file_takes_its_vectors_with_it(store, tmp_path) -> None:
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"])
    indexer = GraphDeltaIndexer(store, parser=FakeParser({}), embedder=FakeEmbedder())
    change = ChangeSet(added=[], modified=[], deleted=["a.py"], current_hashes={})

    indexer.apply(SLUG, tmp_path / "clone", change, commit="c2")

    assert set(_vectors(store)) == {"fileB", "nCaller"}
    assert set(_vectors(store)) - _live_ids(store) == set()


def test_apply_without_an_embedder_still_reaps_the_stale_vectors(store, tmp_path) -> None:
    """No embedder → the scope degrades to BM25, but never to orphaned vectors.

    Keeping the old vectors to preserve recall would rank queries against
    symbols the working tree no longer has, so the retract is unconditional and
    the re-parsed nodes are simply left unvectorised.
    """
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"])
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": _modified_a_py()}))

    delta = indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")

    assert "a.py#newhelper" in delta.added_keys  # the refresh itself succeeded
    live, vectors = _live_ids(store), set(_vectors(store))
    # The retract RAN. Assert the superseded ids are gone by name rather than
    # inferring it from the set differences below: with the retract skipped,
    # the stale nodes stay in BOTH live and vectors and the new nodes are
    # unvectorised either way, so both differences come out identical whether
    # or not the retract happened. Those assertions cannot see this regression.
    assert "fileA" not in live and "nVerify" not in live
    assert "fileA" not in vectors and "nVerify" not in vectors
    assert vectors - live == set()  # (a) holds regardless of the embedder
    assert live - vectors == {"fileA2", "nVerify2", "nHelper"}  # (b) knowingly waived


def test_a_raising_embedder_does_not_fail_the_refresh(store, tmp_path) -> None:
    """An embedding backend that errors degrades the scope, never the apply.

    Same contract the full index states for its own embedding step — retrieval
    falls back to BM25 + graph traversal, which is still useful, so an outage at
    the embedding proxy must not leave the graph itself un-refreshed.
    """
    _seed_two_file_graph(store)
    _seed_vectors(store, ["fileA", "nVerify", "fileB", "nCaller"])
    embedder = FakeEmbedder(fail=True)
    indexer = GraphDeltaIndexer(
        store, parser=FakeParser({"a.py": _modified_a_py()}), embedder=embedder
    )

    warnings: list[str] = []
    sink_id = logger.add(warnings.append, level="WARNING")
    try:
        delta = indexer.apply(SLUG, tmp_path / "clone", _modify_change(), commit="c2")
    finally:
        logger.remove(sink_id)

    assert "a.py#newhelper" in delta.added_keys
    assert embedder.calls, "the embedder was never called"
    assert sum("embeddings unavailable" in line for line in warnings) == 1
    assert set(_vectors(store)) - _live_ids(store) == set()


def test_dangling_nonsynthetic_edge_is_rejected_before_upsert(store, tmp_path) -> None:
    """A CONTAINS edge whose target doesn't resolve (no target_name) is rejected."""
    _seed_two_file_graph(store)
    root = tmp_path / "clone"
    reparse = GraphParseResult(
        nodes=[_node("fileA2", "File", "a.py", "a.py")],
        edges=[GraphEdge(slug=SLUG, source="fileA2", target="ghost", type="CONTAINS")],
        skipped=[],
    )
    indexer = GraphDeltaIndexer(store, parser=FakeParser({"a.py": reparse}))
    change = ChangeSet(
        added=[], modified=["a.py"], deleted=[], current_hashes={"a.py": "hA2"}
    )
    with pytest.raises(ValueError, match="graph schema validation failed"):
        indexer.apply(SLUG, root, change, commit="c2")
