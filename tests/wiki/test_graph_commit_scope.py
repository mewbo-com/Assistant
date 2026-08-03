"""Commit-scoped graph reads — the scope type, the store seam, and the viewer.

The store holds the UNION of every commit ever indexed for a slug, and until
this landed every graph reader read that union. For the knowledge-graph viewer
that meant a repository's deleted code rendered alongside its live code with
nothing distinguishing the two, and the node count grew with re-indexes rather
than with the repository.

Four things are pinned here, each of which was a way to get this wrong:

- ``CommitScope`` keeps the ``None``-means-EXACTLY-NULL convention that
  ``count_graph_nodes`` and ``supersede_graph_artifacts`` already use, instead
  of re-using that parameter name for "unscoped". The parity test below is the
  one that would fail if someone later collapsed the type back into a
  ``commit_sha: str | None`` argument.
- Both store drivers scope identically, by different mechanisms (Mongo narrows
  the query document, JSON filters loaded rows), so they run in lockstep.
- The viewer defaults to the project's own commit.
- Scoping does NOT silently sever the entity→code bridge. An entity anchors by
  raw ``node_id``, and a node id embeds the symbol's byte offset, so an edit
  re-keys it; the view re-anchors those by ``entity_key`` instead of dropping
  them. That repair is the reason this phase could ship without waiting on a
  re-anchoring migration.

Only ``tmp_path``/mongomock are stubbed — the real store and view code runs.
"""

from __future__ import annotations

from pathlib import Path

import mongomock
import pytest
from mewbo_graph.entities.types import Entity, EntityRelation
from mewbo_graph.wiki.graph import KnowledgeGraphView
from mewbo_graph.wiki.memory_types import MemoryEdge, MemoryNode, MemoryProvenance
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.structure_provider import CodeStructureProvider
from mewbo_graph.wiki.types import CommitScope, GraphEdge, Project, make_graph_node

_A = "a" * 40
_B = "b" * 40
SLUG = "org/repo"


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path: Path):
    """Both drivers behind one contract (the idiom in test_artifact_isolation)."""
    if request.param == "json":
        return JsonWikiStore(root_dir=tmp_path / "wiki")
    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_scope")


def _node(nid: str, *, name: str | None = None, file: str = "a.py", typ: str = "Function"):
    return make_graph_node(
        slug=SLUG, node_id=nid, type=typ, name=name or nid, file=file, range=(0, 1)
    )


def _project(commit: str | None) -> Project:
    return Project(
        slug=SLUG, source="github", lang="py", indexedAt="2020-01-01T00:00:00Z",
        pages=0, desc="", commitSha=commit,
    )


# ── the scope type ───────────────────────────────────────────────────────────


def test_at_matches_only_its_own_commit() -> None:
    """``at(sha)`` is an exact match, so another generation's rows fall outside."""
    scope = CommitScope.at(_A)
    assert scope.matches(_A) is True
    assert scope.matches(_B) is False
    assert scope.matches(None) is False


def test_every_matches_everything_including_unstamped() -> None:
    """``every()`` is the pre-scoping behaviour — no row is excluded."""
    scope = CommitScope.every()
    assert scope.matches(_A) is True
    assert scope.matches(None) is True
    assert scope.filter_fields() == {}


def test_at_none_means_exactly_null_not_unscoped(store) -> None:
    """``at(None)`` selects the NULL-stamped generation and excludes real commits.

    This is the trap the type exists to prevent: ``None`` already means "stamped
    NULL" on ``count_graph_nodes``/``supersede_graph_artifacts``, so a
    ``commit_sha=None`` parameter meaning "every commit" would have given one
    name opposite senses on two methods of the same class.
    """
    store.upsert_nodes(SLUG, [_node("stamped")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("unstamped")], commit_sha=None)

    ids = {n.node_id for n in store.query_graph(SLUG, scope=CommitScope.at(None))}
    assert ids == {"unstamped"}


def test_at_none_agrees_with_count_graph_nodes(store) -> None:
    """The scope's NULL convention is the SAME one the count method already uses."""
    store.upsert_nodes(SLUG, [_node("stamped")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("unstamped")], commit_sha=None)

    for commit in (_A, None):
        scoped = len(store.query_graph(SLUG, scope=CommitScope.at(commit)))
        assert scoped == store.count_graph_nodes(SLUG, commit_sha=commit)


# ── the store seam, both drivers ─────────────────────────────────────────────


def test_query_graph_scopes_nodes_to_one_generation(store) -> None:
    """Two generations coexist; a scoped read returns only the one asked for."""
    store.upsert_nodes(SLUG, [_node("old1"), _node("old2")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new1")], commit_sha=_B)

    assert {n.node_id for n in store.query_graph(SLUG, scope=CommitScope.at(_A))} == {
        "old1", "old2",
    }
    assert {n.node_id for n in store.query_graph(SLUG, scope=CommitScope.at(_B))} == {
        "new1",
    }
    assert len(store.query_graph(SLUG, scope=CommitScope.every())) == 3


def test_list_edges_scopes_to_one_generation(store) -> None:
    """Edges scope on the same field, so a view never mixes generations."""
    store.upsert_edges(
        SLUG, [GraphEdge(slug=SLUG, source="old1", target="old2", type="CALLS")],
        commit_sha=_A,
    )
    store.upsert_edges(
        SLUG, [GraphEdge(slug=SLUG, source="new1", target="new2", type="CALLS")],
        commit_sha=_B,
    )

    live = store.list_edges(SLUG, scope=CommitScope.at(_B))
    assert [(e.source, e.target) for e in live] == [("new1", "new2")]
    assert len(store.list_edges(SLUG, scope=CommitScope.every())) == 2


def test_query_graph_filters_by_explicit_node_ids(store) -> None:
    """``node_ids`` fetches a bounded set instead of reading a whole generation."""
    store.upsert_nodes(SLUG, [_node("n1"), _node("n2"), _node("n3")], commit_sha=_A)

    got = store.query_graph(SLUG, scope=CommitScope.every(), node_ids={"n1", "n3"})
    assert {n.node_id for n in got} == {"n1", "n3"}


def test_query_graph_empty_node_ids_returns_nothing(store) -> None:
    """An empty collection means "none of them", never "no filter"."""
    store.upsert_nodes(SLUG, [_node("n1")], commit_sha=_A)

    assert store.query_graph(SLUG, scope=CommitScope.every(), node_ids=set()) == []


def test_neighbors_walk_stays_inside_the_scope(store) -> None:
    """A neighbour walk must not cross into a superseded generation's edges."""
    store.upsert_nodes(SLUG, [_node("seed"), _node("old_nb")], commit_sha=_A)
    store.upsert_edges(
        SLUG, [GraphEdge(slug=SLUG, source="seed", target="old_nb", type="CALLS")],
        commit_sha=_A,
    )
    store.upsert_nodes(SLUG, [_node("seed"), _node("new_nb")], commit_sha=_B)
    store.upsert_edges(
        SLUG, [GraphEdge(slug=SLUG, source="seed", target="new_nb", type="CALLS")],
        commit_sha=_B,
    )

    got = store.query_graph(SLUG, scope=CommitScope.at(_B), neighbors_of="seed")
    assert {n.node_id for n in got} == {"new_nb"}


# ── live_scope ───────────────────────────────────────────────────────────────


def test_live_scope_follows_the_project_commit(store) -> None:
    """The project row names the generation a reader should see."""
    store.create_project(_project(_B))
    assert store.live_scope(SLUG) == CommitScope.at(_B)


def test_live_scope_of_commitless_project_is_every(store) -> None:
    """A commit-less project has ONE generation, so the union IS that generation."""
    store.create_project(_project(None))
    assert store.live_scope(SLUG) == CommitScope.every()


def test_live_scope_without_a_project_row_is_every(store) -> None:
    """No project row ⇒ nothing to scope to; refuse to assert a commit."""
    assert store.live_scope("never/indexed") == CommitScope.every()


# ── the viewer ───────────────────────────────────────────────────────────────


def test_for_slug_shows_only_the_live_commit(store) -> None:
    """The headline fix: a superseded generation no longer renders as live code."""
    store.upsert_nodes(
        SLUG, [_node("gone1", file="deleted.py"), _node("gone2", file="deleted.py")],
        commit_sha=_A,
    )
    store.upsert_nodes(SLUG, [_node("live1", file="kept.py")], commit_sha=_B)
    store.create_project(_project(_B))

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert {n.node_id for n in view.nodes} == {"live1"}
    # "M" in the FE's "showing N of M" must also be the live count, or the
    # banner reports a total the payload can never reach.
    assert view.total_nodes == 1


def test_for_slug_explicit_every_restores_the_union(store) -> None:
    """The union stays reachable for the readers that genuinely need it."""
    store.upsert_nodes(SLUG, [_node("gone1")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("live1")], commit_sha=_B)
    store.create_project(_project(_B))

    view = KnowledgeGraphView.for_slug(store, SLUG, scope=CommitScope.every())

    assert {n.node_id for n in view.nodes} == {"gone1", "live1"}


def test_for_slug_on_commitless_project_is_unchanged(store) -> None:
    """A catalog-style project keeps rendering exactly what it did before."""
    store.upsert_nodes(SLUG, [_node("n1"), _node("n2")], commit_sha=None)
    store.create_project(_project(None))

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert {n.node_id for n in view.nodes} == {"n1", "n2"}


# ── the entity-anchor repair ─────────────────────────────────────────────────


def test_entity_anchor_survives_a_byte_offset_rekey(store) -> None:
    """An anchor into a superseded node re-points at the live symbol of that name.

    A node id embeds the symbol's ``start_byte``, so inserting a line above a
    function re-keys it. The anchor was minted against the old id. Scoping alone
    would drop it — this asserts it is re-anchored by ``entity_key`` instead,
    which is what let commit-scoping ship before a re-anchoring migration.
    """
    store.upsert_nodes(SLUG, [_node("old-id", name="parse", file="a.py")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new-id", name="parse", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))

    # ``Entity.id`` is deterministically derived (sha1 of normalized_name|type),
    # so bind the anchor to the id the model actually minted.
    ent = Entity(name="Parser", type="component")
    store.upsert_entities(SLUG, [ent])
    store.upsert_entity_edges(
        SLUG,
        [EntityRelation(source_id=ent.id, target_id="old-id", type="ANCHORS")],
    )

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert (ent.id, "new-id") in view.cross_edges


def test_entity_anchor_to_deleted_symbol_stays_dropped(store) -> None:
    """An entity pointing at code that no longer exists is dropped, not resurrected.

    The correct outcome: rendering it is the bug commit-scoping set out to fix,
    so the repair must not become a way to bring deleted code back.
    """
    store.upsert_nodes(SLUG, [_node("old-id", name="removed", file="a.py")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new-id", name="kept", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))

    ent = Entity(name="Gone", type="component")
    store.upsert_entities(SLUG, [ent])
    store.upsert_entity_edges(
        SLUG,
        [EntityRelation(source_id=ent.id, target_id="old-id", type="ANCHORS")],
    )

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.cross_edges == ()


def _memory_note(content: str = "a note") -> MemoryNode:
    """A note whose ``node_id`` is whatever the model derives, never a literal.

    ``MemoryNode.node_id`` is recomputed from the note's own content (the
    ``compute_node_id`` idiom, same as ``Entity.id``), so a hand-written id is
    silently discarded — and an anchor edge bound to that literal then matches
    no note, which makes an "anchor is dropped" assertion pass for the wrong
    reason.
    """
    return MemoryNode(
        slug=SLUG, content=content,
        provenance=MemoryProvenance(
            author_agent="test", source="indexer", created_at="2020-01-01T00:00:00Z"
        ),
    )


def test_memory_anchor_resolves_to_the_LIVE_symbol_of_a_shared_key(store) -> None:
    """A memory anchor must land on the live node when both generations share a key.

    A memory ANCHORS edge stores an ``entity_key`` (``file#name``), and after a
    re-index the SAME key exists in two generations under two different node
    ids. ``resolve_many`` returns the first match it walks, so resolving
    against the union makes the answer depend on store iteration order — and
    the view then drops anything that resolved to a superseded id, because it
    requires the target to be in the payload. Live-scoping the resolution is
    what makes this deterministic rather than a coin flip.
    """
    store.upsert_nodes(SLUG, [_node("old-id", name="parse", file="a.py")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new-id", name="parse", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))

    note = _memory_note()
    store.upsert_memory_nodes(SLUG, [note])
    store.upsert_memory_edges(
        SLUG,
        [
            MemoryEdge(
                slug=SLUG, source=note.node_id, target="a.py#parse", type="ANCHORS",
                valid_at="2020-01-01T00:00:00Z",
            )
        ],
    )

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.cross_edges == ((note.node_id, "new-id"),)


def test_memory_anchor_to_a_deleted_symbol_is_dropped(store) -> None:
    """A note about code that no longer exists resolves to nothing, and should."""
    store.upsert_nodes(SLUG, [_node("old-id", name="gone", file="a.py")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new-id", name="kept", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))

    note = _memory_note()
    store.upsert_memory_nodes(SLUG, [note])
    store.upsert_memory_edges(
        SLUG,
        [
            MemoryEdge(
                slug=SLUG, source=note.node_id, target="a.py#gone", type="ANCHORS",
                valid_at="2020-01-01T00:00:00Z",
            )
        ],
    )

    # The note IS collected as an anchor (its source matches a real note); it is
    # the TARGET that resolves to nothing. Asserting emptiness alone would also
    # pass if the edge were never collected at all, so pin the live case above.
    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.cross_edges == ()


def test_entity_key_of_still_reads_every_generation(store) -> None:
    """The named union exception: a node id from a PAST session still labels.

    ``entity_key_of`` maps a node id recorded by an earlier session back to a
    readable key, and a node id embeds the symbol's byte offset — so any edit
    re-keyed it out of the live generation. Scoping this would turn a
    resolvable past-session citation into an ``unknown(...)`` label, which is why
    it is deliberately unscoped. Nothing else asserted this, so the exception
    was unprotected against a later "consistency" cleanup.
    """
    store.upsert_nodes(SLUG, [_node("old-id", name="parse", file="a.py")], commit_sha=_A)
    store.upsert_nodes(SLUG, [_node("new-id", name="parse", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))

    provider = CodeStructureProvider(store)

    assert provider.entity_key_of(SLUG, "old-id") == "a.py#parse"
    # ...while the forward direction answers "where does this key live NOW".
    resolved = provider.resolve(SLUG, "a.py#parse")
    assert resolved is not None
    assert resolved.node_id == "new-id"


def test_kinds_counts_every_class_the_wire_carries(store) -> None:
    """Entity/Memory/Folder are legend entries, not invisible extra nodes.

    The FE builds its whole legend from ``stats.kinds``: it shows a kind only
    when the tally is positive and sums the same map to decide which LAYERS to
    offer. Counting only the AST layer drew those classes on the canvas with no
    legend row and no toggle, so a user could neither name them nor hide them.
    """
    store.upsert_nodes(SLUG, [_node("f1", typ="File", file="a.py")], commit_sha=_B)
    store.upsert_nodes(SLUG, [_node("fn1", name="parse", file="a.py")], commit_sha=_B)
    store.create_project(_project(_B))
    store.upsert_entities(SLUG, [Entity(name="Parser", type="component")])

    wire = KnowledgeGraphView.for_slug(store, SLUG, hierarchy=True).to_wire()
    kinds = wire["stats"]["kinds"]

    assert kinds["Entity"] == 1
    assert kinds["File"] == 1
    assert kinds["Function"] == 1
    # Every kind drawn on the canvas must have a tally, or its toggle is absent.
    drawn = {n["data"]["kind"] for n in wire["nodes"]}
    assert drawn <= set(kinds), f"kinds missing tallies for {drawn - set(kinds)}"


def test_entity_anchor_into_the_live_generation_is_untouched(store) -> None:
    """The common case takes the direct path and never reaches the repair."""
    store.upsert_nodes(SLUG, [_node("live-id", name="parse")], commit_sha=_B)
    store.create_project(_project(_B))

    ent = Entity(name="P", type="component")
    store.upsert_entities(SLUG, [ent])
    store.upsert_entity_edges(
        SLUG,
        [EntityRelation(source_id=ent.id, target_id="live-id", type="ANCHORS")],
    )

    view = KnowledgeGraphView.for_slug(store, SLUG)

    assert view.cross_edges == ((ent.id, "live-id"),)
