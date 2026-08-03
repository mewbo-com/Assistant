"""The eight ``wiki.memory.*`` knobs reach the objects that consume them.

Every knob is a keyword argument whose constructor default EQUALS its config
default, so a mis-wired knob is invisible on a default deployment: the object
behaves correctly and the setting simply does nothing. These tests therefore
set a NON-default value and assert it ARRIVES on the collaborator that reads
it, plus (for the three with an observable effect) that it changes behaviour.

Real config path throughout — the autouse ``app_config_file`` fixture already
points the loader at a temp ``app.json``, so this rewrites that file rather
than patching the accessor. Only the embedder/LLM I/O boundaries are stubbed.
"""
from __future__ import annotations

import pytest
from mewbo_core.config import AppConfig, set_app_config_path
from mewbo_graph.wiki.memory import InsightDeduper, InsightIngestor
from mewbo_graph.wiki.memory_types import (
    MemoryEdge,
    MemoryEmbedding,
    MemoryNode,
    MemoryProvenance,
)
from mewbo_graph.wiki.retriever import HybridRetriever, MultiplexExpander
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import Embedding, GraphEdge, GraphNode, make_graph_node

SLUG = "org/repo"
CLOCK = "2026-06-05T12:00:00Z"


# ── stubs at the I/O boundary ───────────────────────────────────────────────


class FakeEmbedder:
    """Deterministic text→vector stub returning code-shaped Embedding rows."""

    model = "fake"

    def embed_nodes(self, items, *, slug=""):
        return [
            Embedding(slug=slug, node_id=nid, vector=[1.0, 0.0], model="fake", dim=2)
            for nid, _text in items
        ]

    def embed_query(self, text):
        return [1.0, 0.0]


class RecordingStore(JsonWikiStore):
    """JSON store that records the ``k`` each memory kNN search was given."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_search_k: list[int] = []

    def memory_vector_search(self, slug, qvec, *, k=10, filt=None):
        self.memory_search_k.append(k)
        return super().memory_vector_search(slug, qvec, k=k, filt=filt)


# ── fixtures ────────────────────────────────────────────────────────────────


def _gn(nid: str, name: str, f: str) -> GraphNode:
    return make_graph_node(slug=SLUG, node_id=nid, type="Function", name=name, file=f, range=(0, 9))


@pytest.fixture
def store(tmp_path):
    s = RecordingStore(root_dir=tmp_path / "wiki")
    s.upsert_nodes(
        SLUG,
        [
            _gn("fV", "verify", "auth.py"),
            _gn("fS", "save", "store.py"),
            _gn("fL", "load", "store.py"),
        ],
    )
    return s


@pytest.fixture
def memory_config(app_config_file):
    """Rewrite ``wiki.memory.*`` with non-default values and reload config."""

    def _apply(**fields: object) -> None:
        cfg = AppConfig()
        for key, value in fields.items():
            setattr(cfg.wiki.memory, key, value)
        cfg.write(app_config_file)
        # Re-point the loader (which also clears its cache) — ``reset_config``
        # would drop the path override and fall back to the real config chain.
        set_app_config_path(app_config_file)

    return _apply


def _prov() -> MemoryProvenance:
    return MemoryProvenance(author_agent="a", source="indexer", created_at=CLOCK)


def _seed_note(store, content: str, anchor: str) -> MemoryNode:
    node = MemoryNode(slug=SLUG, content=content, provenance=_prov())
    store.upsert_memory_nodes(SLUG, [node])
    store.upsert_memory_embeddings(
        SLUG,
        [MemoryEmbedding(slug=SLUG, node_id=node.node_id, vector=[1.0, 0.0], model="m", dim=2)],
    )
    store.upsert_memory_edges(
        SLUG,
        [
            MemoryEdge(
                slug=SLUG, source=node.node_id, target=anchor, type="ANCHORS", valid_at=CLOCK
            )
        ],
    )
    return node


# ── ingestor + deduper: five knobs, read at InsightIngestor.from_store ──────


def test_dedup_knobs_reach_the_deduper(store, memory_config) -> None:
    memory_config(dedup_k=11, dedup_cosine=0.42, fuzzy_jaccard=0.5)
    deduper = InsightIngestor.from_store(store, embedder=FakeEmbedder())._deduper
    assert deduper._dedup_k == 11
    assert deduper._dedup_cosine == 0.42
    assert deduper._fuzzy_jaccard == 0.5


def test_dedup_k_bounds_the_knn_search_the_deduper_issues(store, memory_config) -> None:
    memory_config(dedup_k=11)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), clock=lambda: CLOCK)
    ing.ingest(SLUG, "Sessions expire after one hour")
    assert store.memory_search_k == [11]


def test_explicit_deduper_still_overrides_config(store, memory_config) -> None:
    memory_config(dedup_k=11)
    injected = InsightDeduper(store=store, dedup_k=3)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), deduper=injected)
    assert ing._deduper is injected
    assert ing._deduper._dedup_k == 3


def test_max_anchors_reaches_the_ingestor_and_caps_anchors(store, memory_config) -> None:
    memory_config(max_anchors=2)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), clock=lambda: CLOCK)
    assert ing._max_anchors == 2
    res = ing.ingest(
        SLUG,
        "The store module persists and reloads graph nodes",
        anchors=["auth.py#verify", "store.py#save", "store.py#load"],
    )
    [claim] = res.claims
    assert claim.anchors == ["auth.py#verify", "store.py#save"]
    assert "anchors capped to 2" in claim.warnings


def test_explicit_max_anchors_still_overrides_config(store, memory_config) -> None:
    memory_config(max_anchors=2)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), max_anchors=7)
    assert ing._max_anchors == 7


def test_max_insight_chars_reaches_the_ingestor_and_rejects_a_longer_claim(
    store, memory_config
) -> None:
    memory_config(max_insight_chars=40)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), clock=lambda: CLOCK)
    assert ing._max_chars == 40
    [claim] = ing.ingest(SLUG, "x" * 41).claims
    assert claim.action == "rejected"
    assert "claim exceeds 40 chars" in claim.warnings


def test_explicit_max_chars_still_overrides_config(store, memory_config) -> None:
    memory_config(max_insight_chars=40)
    ing = InsightIngestor.from_store(store, embedder=FakeEmbedder(), max_chars=90)
    assert ing._max_chars == 90


# ── expander: three knobs, read at MultiplexExpander.from_store ─────────────


def test_fusion_knobs_reach_the_expander(store, memory_config) -> None:
    memory_config(fusion_w_ppr=0.75, hub_degree=3, expansion_hops=2)
    exp = MultiplexExpander.from_store(store)
    assert exp.w_ppr == 0.75
    assert exp.hub_degree == 3
    assert exp.expansion_hops == 2


def test_fusion_knobs_reach_the_expander_the_retriever_builds(store, memory_config) -> None:
    memory_config(fusion_w_ppr=0.75, hub_degree=3, expansion_hops=2)
    _seed_note(store, "Sessions expire after one hour", "auth.py#verify")
    retriever = HybridRetriever(store=store, embedder=FakeEmbedder())
    retriever.search(SLUG, "session expiry", k=5, sources="pages", memory_expand=True)
    assert retriever._expander is not None
    assert (
        retriever._expander.w_ppr,
        retriever._expander.hub_degree,
        retriever._expander.expansion_hops,
    ) == (0.75, 3, 2)


def test_fusion_w_ppr_scales_the_anchored_code_hit(store, memory_config) -> None:
    memory_config(fusion_w_ppr=0.75)
    _seed_note(store, "Sessions expire after one hour", "auth.py#verify")
    hits = MultiplexExpander.from_store(store).expand(SLUG, [1.0, 0.0], k=5)
    seed = next(h for h in hits if h.kind == "memory")
    anchored = next(h for h in hits if h.kind == "node" and h.id == "fV")
    assert anchored.score == pytest.approx(0.75 * seed.score)


def test_hub_degree_decides_which_anchor_is_damped(store, memory_config) -> None:
    """``fV`` (degree 2) is a hub at ``hub_degree=1`` and is not at the default 50."""
    store.upsert_edges(
        SLUG,
        [
            GraphEdge(slug=SLUG, source="fV", target="fS", type="CALLS"),
            GraphEdge(slug=SLUG, source="fV", target="fL", type="CALLS"),
        ],
    )
    _seed_note(store, "Sessions expire after one hour", "auth.py#verify")

    memory_config(hub_degree=50)
    undamped = MultiplexExpander.from_store(store).expand(SLUG, [1.0, 0.0], k=5)
    memory_config(hub_degree=1)
    damped = MultiplexExpander.from_store(store).expand(SLUG, [1.0, 0.0], k=5)

    def _anchor_score(hits):
        return next(h.score for h in hits if h.kind == "node" and h.id == "fV")

    assert _anchor_score(damped) == pytest.approx(_anchor_score(undamped) / 2)


def test_expansion_hops_bounds_the_structural_walk(store, memory_config) -> None:
    memory_config(expansion_hops=0)
    _seed_note(store, "Sessions expire after one hour", "auth.py#verify")
    hits = MultiplexExpander.from_store(store).expand(SLUG, [1.0, 0.0], k=5)
    assert not [h for h in hits if h.metadata.get("expanded")]


def test_explicit_fusion_knobs_still_override_config(store, memory_config) -> None:
    memory_config(fusion_w_ppr=0.75, hub_degree=3, expansion_hops=2)
    exp = MultiplexExpander.from_store(store, w_ppr=0.2, hub_degree=9, expansion_hops=4)
    assert (exp.w_ppr, exp.hub_degree, exp.expansion_hops) == (0.2, 9, 4)


def test_injected_expander_is_not_rebuilt_from_config(store, memory_config) -> None:
    memory_config(fusion_w_ppr=0.75, hub_degree=3, expansion_hops=2)
    _seed_note(store, "Sessions expire after one hour", "auth.py#verify")
    injected = MultiplexExpander(store=store, w_ppr=0.2, hub_degree=9, expansion_hops=4)
    retriever = HybridRetriever(store=store, embedder=FakeEmbedder(), expander=injected)
    retriever.search(SLUG, "session expiry", k=5, sources="pages", memory_expand=True)
    assert retriever._expander is injected
    assert injected.w_ppr == 0.2
