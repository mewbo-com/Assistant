"""Tier-2 proof: a store write path's round-trip count, measured on real Mongo.

This is the first test in the repository to assert a COST rather than a result,
and it is here rather than in tier 1 because it cannot exist in tier 1:
mongomock accepts ``event_listeners=`` and implements none of PyMongo's
monitoring API, so the number under assertion is unobservable against it.

The subject is ``MongoWikiStore.upsert_nodes``, whose docstring declares its
round-trip count is ``O(nodes / batch)``. The assertion is on the SCALING, not
on a literal: ten times the documents must not cost ten times the commands.
Written that way it survives a change to the batch size — and it fails the day
someone reintroduces a per-document loop, which is exactly the defect class a
correctness suite returns green for, since the rows written are identical
either way.
"""

from __future__ import annotations

import pytest
from mewbo_graph.wiki.store import MongoWikiStore
from mewbo_graph.wiki.types import FunctionNode
from real_mongo import CommandCounter, RealMongoTier, TierClient

#: The two document counts, an order of magnitude apart. Both sit under the
#: store's batch bound, so a batched write costs the SAME number of commands
#: for either — while a per-document write costs ten times as many for the
#: larger. Scale the axis whose cost is in question; a test using 4 and 8
#: documents cannot tell the two implementations apart.
SMALL = 40
LARGE = 400


def _nodes(slug: str, count: int) -> list[FunctionNode]:
    """*count* distinct graph nodes for *slug*."""
    return [
        FunctionNode(
            slug=slug,
            node_id=f"{slug}:fn:{i}",
            type="Function",
            name=f"fn_{i}",
            file=f"src/mod_{i}.py",
            range=(i, i + 1),
        )
        for i in range(count)
    ]


def _write_commands(
    store: MongoWikiStore, counter: CommandCounter, slug: str, count: int
) -> int:
    """Issue one ``upsert_nodes`` of *count* nodes; return the commands it cost."""
    counter.reset()
    store.upsert_nodes(slug, _nodes(slug, count))
    return counter.writes


@pytest.mark.realmongo
def test_upsert_nodes_round_trips_do_not_scale_with_document_count(
    real_mongo_client: TierClient,
    real_mongo_commands: CommandCounter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ten times the nodes must not cost ten times the round trips."""
    store = MongoWikiStore(
        client=real_mongo_client, database=RealMongoTier.DATABASE
    )
    # Warm-up on its own slug: the first upsert also builds the graph indexes,
    # and an index build is not part of the cost being pinned.
    _write_commands(store, real_mongo_commands, "warmup", 1)

    small = _write_commands(store, real_mongo_commands, "small", SMALL)
    large = _write_commands(store, real_mongo_commands, "large", LARGE)

    with capsys.disabled():
        print(
            f"\nupsert_nodes write commands: {SMALL} nodes -> {small}, "
            f"{LARGE} nodes -> {large} "
            f"(linear would be ~{small * (LARGE // SMALL)})"
        )

    assert small >= 1, "the measurement is vacuous if no write command was issued"
    # Ten times the documents, at most twice the commands. The generous factor
    # is deliberate: it leaves a batched implementation free to split a write
    # however it likes, while a per-document loop misses by a factor of five.
    assert large <= small * 2, (
        f"round trips scale with document count: {small} commands for {SMALL} "
        f"nodes but {large} for {LARGE}"
    )

    # The rows are still there — a cost assertion that a no-op would satisfy
    # proves nothing about the write path.
    db = real_mongo_client[RealMongoTier.DATABASE]
    assert db["wiki_graph_nodes"].count_documents({"slug": "large"}) == LARGE


def test_tier_skips_cleanly_when_the_container_is_absent() -> None:
    """No Docker, no error: an unreachable tier is a skip, and it is fast.

    Tier 1 is the common loop and must never require a container. This drives
    the REAL ``MongoClient`` factory (not an injected fake) against a port
    nothing listens on, because the default factory is the thing whose failure
    mode is in question.
    """
    tier = RealMongoTier(uri="mongodb://127.0.0.1:1")
    with pytest.raises(pytest.skip.Exception) as excinfo:
        tier.client_or_skip()
    assert "make test-mongo" in str(excinfo.value)


@pytest.mark.realmongo
def test_duplicate_key_in_one_batch_resolves_to_the_last_write(
    real_mongo_client: TierClient, real_mongo_commands: CommandCounter
) -> None:
    """Two writes to one key in one batch must leave the LAST one, on a real server.

    ``ordered=False`` permits MongoDB to execute a batch's operations in any
    order, so this property cannot come from the driver — the store folds a
    batch by filter and keeps the last write. Worth asserting against a real
    server rather than only the double: mongomock applies a bulk list in order,
    so it would report this green whether or not the fold exists. The command
    count is the half that fails when the fold is removed.
    """
    store = MongoWikiStore(
        client=real_mongo_client, database=RealMongoTier.DATABASE
    )
    slug = "dup"
    nodes = [
        FunctionNode(
            slug=slug, node_id=f"{slug}:fn:0", type="Function", name="fn_0",
            file="src/mod_0.py", range=(0, 1), docstring=mark,
        )
        for mark in ("first", "second", "third")
    ]
    real_mongo_commands.reset()
    store.upsert_nodes(slug, nodes)

    db = real_mongo_client[RealMongoTier.DATABASE]
    stored = list(db["wiki_graph_nodes"].find({"slug": slug}))
    assert len(stored) == 1, "the unique index must leave exactly one row"
    assert stored[0]["docstring"] == "third", "the last write must win"
    assert real_mongo_commands.writes == 1, (
        "three ops on one key must cost one write command, not three"
    )
