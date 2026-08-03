"""The two storable session facets — the project SET and the pin stamp.

Four layers, one feature, in the order a change moves through them:

1. ``ProjectIdentity`` and ``SessionQuery`` as PURE logic — no store, no clock.
2. The two drivers agreeing over the same seeded state. ``matches_record`` and
   ``to_mongo`` are two spellings of one predicate, so they are exercised as a
   SEAM (identical facets in, identical id set out) rather than by a hand-written
   evaluator that would encode a test author's belief about what Mongo does.
3. The store behaviour that only the real filesystem driver can show — a session
   ACCUMULATING projects as its context switches, and the append surviving a
   facet write that fails.
4. The properties a caller actually buys: a listing opens no transcript at all
   (filtered or not — the digest/record split landed on ``main`` the same day
   this facet did, and the two compose), and a pin orders without ever
   bypassing a filter.

The cost test asserts a READ COUNT rather than a duration. A timing assertion on
a filesystem read is flaky by construction; the count is the honest form of the
same claim, and it fails loudly the day a predicate moves back out of the store.
"""

from contextlib import contextmanager
from unittest.mock import patch

import mongomock
import pytest
from loguru import logger as loguru_logger
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_query import SessionQuery
from mewbo_core.session.session_store import SessionStore
from mewbo_core.session.session_store_mongo import MongoSessionStore
from mewbo_core.workspaces.project_identity import AUTO_PROJECT, ProjectIdentity
from pydantic import ValidationError

ALICE = "user:alice"
BOB = "user:bob"


@contextmanager
def _capture_loguru(level: str = "WARNING"):
    """Capture loguru records emitted inside the block.

    The store logs through loguru (``mewbo_core.common.get_logger``), which
    pytest's ``caplog`` never sees — a temporary sink is loguru's own documented
    way to assert on an emitted message.
    """
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level=level)
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


@pytest.fixture()
def store(tmp_path):
    """A filesystem-backed store rooted in a throwaway directory."""
    return SessionStore(root_dir=str(tmp_path / "sessions"))


@pytest.fixture()
def mongo_store(tmp_path):
    """A Mongo-backed store on mongomock, mirroring ``test_session_ownership``."""
    with patch(
        "mewbo_core.session.session_store_mongo.MongoClient",
        mongomock.MongoClient,
    ):
        return MongoSessionStore(
            root_dir=str(tmp_path / "mongo"),
            uri="mongodb://localhost:27017",
            database="test_facets",
        )


def _context(store_, session_id: str, payload: dict) -> None:
    """Append a context event the way every real producer does."""
    store_.append_event(session_id, {"type": "context", "payload": payload})


def _with_user_turn(store_, owner: str | None = None) -> str:
    """Mint a session carrying one user event so it survives the list filters."""
    session_id = store_.create_session(owner)
    store_.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    return session_id


# ---------------------------------------------------------------------------
# 1a. ProjectIdentity — the rule, with no store in sight
# ---------------------------------------------------------------------------


def test_the_auto_sentinel_declares_no_project():
    """``auto`` means "not decided yet", which is not a project name.

    Were it stored, ``?project=auto`` would look like a real query and would
    file every not-yet-decided session under one name.
    """
    assert ProjectIdentity.is_auto(AUTO_PROJECT)
    assert ProjectIdentity.normalize(AUTO_PROJECT) is None
    assert ProjectIdentity.from_context({"project": AUTO_PROJECT}) is None


def test_the_sentinel_is_recognised_through_surrounding_whitespace():
    """A context payload is written by many producers; one pads its value."""
    assert ProjectIdentity.is_auto("  auto  ")
    assert ProjectIdentity.normalize("  auto  ") is None


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", 7, 0, True, ["repo"], {"repo": "x"}, object()],
    ids=["none", "empty", "blank", "int", "zero", "bool", "list", "dict", "object"],
)
def test_a_blank_or_non_string_value_declares_nothing(value):
    """The rule runs on the append hot path over payloads it does not control."""
    assert ProjectIdentity.normalize(value) is None


def test_the_managed_prefix_is_stripped_from_a_stored_identity():
    """``managed:<uuid>`` is high-cardinality and useless as a filter value."""
    assert ProjectIdentity.normalize("managed:9f2c1b") == "9f2c1b"
    assert ProjectIdentity.from_context({"project": "managed:9f2c1b"}) == "9f2c1b"


def test_a_bare_managed_prefix_declares_nothing():
    """Stripping the prefix off a prefix-only value leaves nothing to record."""
    assert ProjectIdentity.normalize("managed:") is None
    assert ProjectIdentity.normalize("managed:   ") is None


def test_the_managed_prefix_is_stripped_from_a_requested_name_too():
    """Normalizing BOTH sides is what lets a caller pass the prefixed form.

    A surface holding a worktree-backed session knows it as ``managed:<uuid>``;
    the identity recorded from that context was the bare uuid. Only normalizing
    the stored side would make that request match nothing.
    """
    assert ProjectIdentity.matches("managed:9f2c1b", ["9f2c1b"])
    assert ProjectIdentity.matches("9f2c1b", ["9f2c1b"])
    assert ProjectIdentity.matches("  9f2c1b  ", ["9f2c1b"])


def test_repo_wins_over_project():
    """``repo`` names the underlying repository; ``project`` may be a local alias."""
    assert (
        ProjectIdentity.from_context({"project": "local-alias", "repo": "acme/beacon"})
        == "acme/beacon"
    )


@pytest.mark.parametrize("repo", [None, "", "   ", AUTO_PROJECT, "managed:"])
def test_project_is_used_when_repo_declares_nothing(repo):
    """Precedence is over what each key DECLARES, not over which key is present.

    A ``repo`` that normalizes to nothing must fall through to ``project``
    rather than short-circuiting the lookup at a key that said nothing.
    """
    assert ProjectIdentity.from_context({"repo": repo, "project": "acme/beacon"}) == "acme/beacon"


def test_a_context_declaring_neither_key_yields_nothing():
    """The overwhelmingly common shape on the append path."""
    assert ProjectIdentity.from_context({}) is None
    assert ProjectIdentity.from_context({"client_capabilities": ["wiki"]}) is None


def test_a_request_that_normalizes_to_nothing_matches_nothing():
    """``?project=auto`` can never be a real query — not even against ``auto``.

    Asserted against a stored list literally containing ``"auto"``, which the
    recording side can never produce, so the guard is proven on the REQUEST side
    where a URL parameter actually reaches it.
    """
    assert not ProjectIdentity.matches(AUTO_PROJECT, ["auto"])
    assert not ProjectIdentity.matches("", ["acme/beacon"])
    assert not ProjectIdentity.matches("   ", ["acme/beacon"])


def test_a_session_with_no_recorded_projects_matches_nothing():
    """The ordinary shape for every session predating the facet."""
    assert not ProjectIdentity.matches("acme/beacon", [])


# ---------------------------------------------------------------------------
# 1b. SessionQuery — the predicate, compiled two ways
# ---------------------------------------------------------------------------


def test_requested_projects_are_normalized_and_deduped_at_definition():
    """Validating here means no clause that can never match reaches a driver."""
    query = SessionQuery(
        projects=["managed:9f2c1b", "9f2c1b", "  acme/beacon  ", "", AUTO_PROJECT, "   "]
    )
    assert query.projects == ["9f2c1b", "acme/beacon"]


def test_normalization_preserves_the_order_a_caller_asked_in():
    """Dedup keeps the FIRST occurrence — a stable, explicable ordering."""
    assert SessionQuery(projects=["b", "a", "b"]).projects == ["b", "a"]


def test_a_query_of_only_unusable_names_narrows_on_projects_at_all():
    """Every name dropped ⇒ no clause, NOT a clause matching the empty set.

    The alternative — an ``$in: []`` that matches nothing — would turn
    ``?project=auto`` into a listing that silently returns zero sessions.
    """
    query = SessionQuery(projects=[AUTO_PROJECT, "", "  "])
    assert query.projects == []
    assert query.is_unfiltered
    assert "projects" not in query.to_mongo()


def test_the_query_refuses_an_unknown_field():
    """``extra="forbid"`` turns a caller's typo into a 400 rather than a no-op."""
    with pytest.raises(ValidationError):
        SessionQuery(project="acme/beacon")


def test_is_unfiltered_ignores_the_archived_default():
    """``include_archived`` is a default, not a narrowing a caller asked for."""
    assert SessionQuery().is_unfiltered
    assert SessionQuery(include_archived=True).is_unfiltered
    assert not SessionQuery(owner=ALICE).is_unfiltered
    assert not SessionQuery(pinned=True).is_unfiltered
    assert not SessionQuery(pinned=False).is_unfiltered
    assert not SessionQuery(projects=["acme/beacon"]).is_unfiltered


def test_to_mongo_default_excludes_archived_and_narrows_nothing_else():
    """The whole compiled document, not just the clause under discussion."""
    assert SessionQuery().to_mongo() == {"archived_at": None}


def test_to_mongo_include_archived_drops_the_clause_entirely():
    """An unfiltered, archive-inclusive query must match every document."""
    assert SessionQuery(include_archived=True).to_mongo() == {}


def test_to_mongo_owner_selects_the_subject_or_an_unowned_session():
    """Equality to ``None`` is load-bearing: Mongo matches a MISSING field too.

    Asserted as the literal clause rather than via ``$exists`` because that is
    the property the migration semantic rests on — a session predating the field
    has no ``owner_subject`` key at all.
    """
    assert SessionQuery(owner=ALICE, include_archived=True).to_mongo() == {
        "$or": [{"owner_subject": ALICE}, {"owner_subject": None}]
    }


def test_to_mongo_pinned_true_selects_a_stamped_document():
    """Unpinning ``$unset``s the field, so "pinned" is "the stamp exists"."""
    assert SessionQuery(pinned=True, include_archived=True).to_mongo() == {
        "pinned_at": {"$ne": None}
    }


def test_to_mongo_pinned_false_selects_missing_as_well_as_null():
    """Same equality-to-``None`` trick — no session was ever backfilled to null."""
    assert SessionQuery(pinned=False, include_archived=True).to_mongo() == {"pinned_at": None}


def test_to_mongo_projects_is_a_membership_clause_over_the_array():
    """A session matches if ANY project it worked in was asked for."""
    query = SessionQuery(projects=["acme/beacon", "managed:9f2c1b"], include_archived=True)
    assert query.to_mongo() == {"projects": {"$in": ["acme/beacon", "9f2c1b"]}}


def test_to_mongo_compiles_every_field_at_once():
    """The combination, so a clause cannot quietly overwrite a sibling's key."""
    query = SessionQuery(owner=ALICE, pinned=True, projects=["acme/beacon"])
    assert query.to_mongo() == {
        "$or": [{"owner_subject": ALICE}, {"owner_subject": None}],
        "archived_at": None,
        "pinned_at": {"$ne": None},
        "projects": {"$in": ["acme/beacon"]},
    }


# ---------------------------------------------------------------------------
# 2. The two drivers mean the same thing
# ---------------------------------------------------------------------------

# (label, owner, archived, pinned, projects) — one row per interesting shape.
FACET_MATRIX = [
    ("bare", None, False, False, []),
    ("alice_beacon", ALICE, False, False, ["acme/beacon"]),
    ("alice_beacon_pinned", ALICE, False, True, ["acme/beacon"]),
    ("bob_beacon", BOB, False, False, ["acme/beacon"]),
    ("bob_ledger_pinned", BOB, False, True, ["acme/ledger"]),
    ("alice_archived_beacon", ALICE, True, False, ["acme/beacon"]),
    ("alice_archived_pinned", ALICE, True, True, ["acme/ledger"]),
    ("alice_two_projects", ALICE, False, False, ["acme/beacon", "acme/ledger"]),
]

QUERY_MATRIX = [
    SessionQuery(),
    SessionQuery(include_archived=True),
    SessionQuery(owner=ALICE),
    SessionQuery(owner=ALICE, include_archived=True),
    SessionQuery(owner=BOB, include_archived=True),
    SessionQuery(pinned=True, include_archived=True),
    SessionQuery(pinned=False, include_archived=True),
    SessionQuery(projects=["acme/beacon"], include_archived=True),
    SessionQuery(projects=["acme/ledger"], include_archived=True),
    SessionQuery(projects=["acme/beacon", "acme/ledger"], include_archived=True),
    SessionQuery(projects=["acme/nothing"], include_archived=True),
    SessionQuery(owner=ALICE, pinned=True, projects=["acme/ledger"], include_archived=True),
    SessionQuery(owner=BOB, pinned=True, projects=["acme/beacon"]),
]


def _seed_facets(store_) -> dict[str, str]:
    """Write ``FACET_MATRIX`` into *store_*, returning ``session_id → label``."""
    labels: dict[str, str] = {}
    for label, owner, archived, pinned, projects in FACET_MATRIX:
        session_id = store_.create_session(owner)
        labels[session_id] = label
        for project in projects:
            store_.record_project(session_id, project)
        if pinned:
            store_.set_pinned(session_id, True)
        if archived:
            store_.archive_session(session_id)
    return labels


@pytest.mark.parametrize("query", QUERY_MATRIX, ids=lambda q: str(q.to_mongo()))
def test_both_drivers_select_the_same_sessions(store, mongo_store, query):
    """``matches_record`` and ``to_mongo`` are one predicate spelled twice.

    A driver-local test cannot catch a divergence here: each side would assert
    the shape it implements, and the two would drift while both stayed green.
    Seeding identical facets and comparing the SELECTED SET is the seam.
    """
    json_labels = _seed_facets(store)
    mongo_labels = _seed_facets(mongo_store)

    from_json = {json_labels[sid] for sid in store.query_sessions(query)}
    from_mongo = {mongo_labels[sid] for sid in mongo_store.query_sessions(query)}

    assert from_json == from_mongo


def test_the_driver_parity_matrix_is_not_vacuous(store):
    """A parity assertion over queries that all select everything proves nothing.

    So the matrix is pinned to actually discriminate: at least one query must
    select a proper non-empty subset, and the queries must not all agree.
    """
    labels = _seed_facets(store)
    selections = {
        str(query.to_mongo()): frozenset(labels[sid] for sid in store.query_sessions(query))
        for query in QUERY_MATRIX
    }
    everything = frozenset(labels.values())
    assert any(selection and selection != everything for selection in selections.values())
    assert len(set(selections.values())) > 1


def test_a_mongo_document_predating_the_facets_still_lists(mongo_store):
    """The real shape on a running deployment: the fields do not exist at all.

    ``$setOnInsert`` never wrote ``projects`` or ``pinned_at``, so every session
    already in the collection lacks both keys rather than holding null. An
    unfiltered listing must still see it, an unpinned-only query must select it,
    and a project query must not.
    """
    session_id = mongo_store.create_session(ALICE)
    mongo_store._col("sessions").update_one(
        {"_id": session_id},
        {"$unset": {"projects": "", "pinned_at": "", "archived_at": ""}},
    )

    assert session_id in mongo_store.query_sessions(SessionQuery())
    assert session_id in mongo_store.query_sessions(SessionQuery(pinned=False))
    assert session_id not in mongo_store.query_sessions(SessionQuery(pinned=True))
    assert session_id not in mongo_store.query_sessions(SessionQuery(projects=["acme/beacon"]))


def test_a_json_index_predating_the_facets_still_lists(store):
    """The filesystem mirror of the same migration case — no buckets at all."""
    session_id = store.create_session(ALICE)
    store._save_index({"tags": {}, "archived": {}, "terminated": {}})

    assert store.query_sessions(SessionQuery()) == [session_id]
    assert store.query_sessions(SessionQuery(pinned=False)) == [session_id]
    assert store.query_sessions(SessionQuery(pinned=True)) == []
    assert store.query_sessions(SessionQuery(projects=["acme/beacon"])) == []
    assert store.projects_for_session(session_id) == []
    assert store.get_pinned_at(session_id) is None


# ---------------------------------------------------------------------------
# 3. The store, against the real filesystem driver
# ---------------------------------------------------------------------------


def test_a_session_that_switches_project_accumulates_both(store):
    """THE case the facet exists for: an auto-select session that moved.

    It starts bound to nothing, the agent picks one, and a mid-task switch binds
    another. A single-valued field would find it under only the last one, so a
    user filtering by the project they watched it work in would not see it.
    """
    session_id = store.create_session()
    _context(store, session_id, {"project": AUTO_PROJECT})
    _context(store, session_id, {"project": "acme/beacon"})
    _context(store, session_id, {"project": "acme/ledger"})

    assert store.projects_for_session(session_id) == ["acme/beacon", "acme/ledger"]
    assert session_id in store.query_sessions(SessionQuery(projects=["acme/beacon"]))
    assert session_id in store.query_sessions(SessionQuery(projects=["acme/ledger"]))


def test_the_auto_sentinel_is_never_recorded(store):
    """A session that never chose a project has an EMPTY set, not a placeholder."""
    session_id = store.create_session()
    _context(store, session_id, {"project": AUTO_PROJECT})

    assert store.projects_for_session(session_id) == []
    assert "projects" not in store._load_index() or not store._load_index()["projects"]


def test_recording_is_idempotent_across_re_emitted_context(store):
    """Every turn of a bound session re-emits the same context event."""
    session_id = store.create_session()
    for _ in range(5):
        _context(store, session_id, {"project": "acme/beacon"})

    assert store.projects_for_session(session_id) == ["acme/beacon"]


def test_the_managed_form_is_recorded_bare_and_matched_either_way(store):
    """A worktree-backed session is addressed two ways and must match both."""
    session_id = store.create_session()
    _context(store, session_id, {"project": "managed:9f2c1b"})

    assert store.projects_for_session(session_id) == ["9f2c1b"]
    assert session_id in store.query_sessions(SessionQuery(projects=["managed:9f2c1b"]))
    assert session_id in store.query_sessions(SessionQuery(projects=["9f2c1b"]))


def test_only_a_context_event_binds_a_project(store):
    """A ``repo``-carrying payload on a non-context event is not a binding."""
    session_id = store.create_session()
    store.append_event(session_id, {"type": "user", "payload": {"repo": "acme/beacon"}})
    store.append_event(session_id, {"type": "context", "payload": "not-a-dict"})

    assert store.projects_for_session(session_id) == []


class _BrokenProjectFacet(SessionStore):
    """A store whose project facet write always fails.

    Models the real failure — a Mongo write erroring on the append hot path —
    without needing one, because the hook's contract is about the CALLER's path,
    not about which backend broke.
    """

    def record_project(self, session_id: str, project: str) -> None:
        """Fail every facet write."""
        raise RuntimeError("facet backend is unavailable")


def test_a_failing_project_facet_never_breaks_the_append(tmp_path):
    """The facet is best-effort: losing one costs a filter hit, not a transcript.

    Raising here would take down the append the hook rode in on — which is every
    context event, on every session, on the hot path.
    """
    store = _BrokenProjectFacet(root_dir=str(tmp_path / "broken"))
    session_id = store.create_session()

    with _capture_loguru() as messages:
        _context(store, session_id, {"project": "acme/beacon"})
        store.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})

    events = store.load_transcript(session_id)
    assert [event["type"] for event in events] == ["context", "user"]
    assert events[0]["payload"] == {"project": "acme/beacon"}
    # Swallowed, but never silent — a facet quietly failing forever is how a
    # filter comes to under-report with nothing to look at.
    assert any("project facet" in message for message in messages)


def test_pinning_round_trips_and_the_query_follows(store):
    """Pin, read back, unpin, read back — and the filter agrees at each step."""
    session_id = store.create_session()

    assert store.get_pinned_at(session_id) is None
    assert store.query_sessions(SessionQuery(pinned=True)) == []

    store.set_pinned(session_id, True)
    stamp = store.get_pinned_at(session_id)
    assert isinstance(stamp, str) and stamp
    assert store.query_sessions(SessionQuery(pinned=True)) == [session_id]
    assert store.query_sessions(SessionQuery(pinned=False)) == []

    store.set_pinned(session_id, False)
    assert store.get_pinned_at(session_id) is None
    assert store.query_sessions(SessionQuery(pinned=True)) == []
    assert store.query_sessions(SessionQuery(pinned=False)) == [session_id]


def test_unpinning_a_session_that_was_never_pinned_is_a_no_op(store):
    """The stamp is reversible, so the clear path is reachable with nothing to clear."""
    session_id = store.create_session()
    store.set_pinned(session_id, False)

    assert store.get_pinned_at(session_id) is None


def test_query_sessions_excludes_archived_while_list_sessions_includes_it(store):
    """The backward-compat property several API callers depend on.

    ``list_sessions`` has ALWAYS returned archived sessions, with the archived
    filter applied by its caller. Pushing the filter into the store must not
    change that: the delegate passes ``include_archived=True`` precisely so the
    established call shape keeps its established meaning.
    """
    live = store.create_session()
    archived = store.create_session()
    store.archive_session(archived)

    assert set(store.query_sessions(SessionQuery())) == {live}
    assert set(store.query_sessions(SessionQuery(include_archived=True))) == {live, archived}
    assert set(store.list_sessions()) == {live, archived}


def test_the_legacy_list_sessions_still_narrows_by_owner(store):
    """The delegate's other argument survives the rewrite."""
    mine = store.create_session(ALICE)
    theirs = store.create_session(BOB)
    store.archive_session(mine)

    listed = store.list_sessions(ALICE)
    assert mine in listed
    assert theirs not in listed


# ---------------------------------------------------------------------------
# 4a. The cost characteristic — a filtered listing opens no rejected transcript
# ---------------------------------------------------------------------------


class _CountingStore(SessionStore):
    """A filesystem store that tallies calls to the reads a listing can make.

    Started life counting only ``load_transcript`` — a listing built each row
    by loading a session's WHOLE transcript, so that was the cost to prove a
    predicate skipped. That call is gone from the listing path entirely now
    (``list_session_digests``/``session_digest`` replaced it, landing on
    ``main`` the same day this facet did), which makes the claim this class
    exists to support STRONGER, not weaker: a listing opens no transcript at
    all, filtered or not. What is worth counting now is the batched calls
    themselves — ``list_session_digests``/``load_session_records`` must stay
    ONE call each regardless of row count, the same property a sibling suite
    (``test_session_listing_cost.py``) pins for the unfiltered case.
    """

    def __init__(self, root_dir: str) -> None:
        """Initialise the store with empty call tallies."""
        super().__init__(root_dir=root_dir)
        self.transcript_reads: list[str] = []
        self.calls: dict[str, int] = {}

    def _tally(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def load_transcript(self, session_id: str):
        """Record the read, then perform it for real."""
        self.transcript_reads.append(session_id)
        return super().load_transcript(session_id)

    def list_session_digests(self, query=None, *, limit=None, offset=0):
        self._tally("list_session_digests")
        return super().list_session_digests(query, limit=limit, offset=offset)

    def load_session_records(self, session_ids: list[str]):
        self._tally("load_session_records")
        return super().load_session_records(session_ids)


def _seed_project_sessions(store_, project: str, count: int) -> list[str]:
    """Create *count* listable sessions bound to *project*."""
    ids = []
    for _ in range(count):
        session_id = store_.create_session()
        _context(store_, session_id, {"project": project})
        store_.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
        ids.append(session_id)
    return ids


@pytest.mark.parametrize("noise", [3, 30], ids=["few-non-matching", "many-non-matching"])
def test_a_filtered_listing_opens_no_transcript_and_batches_its_two_reads(tmp_path, noise):
    """The listing-cost property this facet actually buys, stated honestly.

    Building a row by loading that session's WHOLE transcript makes a
    predicate applied after the fact read a session only to throw it away. No
    such call exists on the listing path: it is two BATCHED reads
    (``list_session_digests``/``load_session_records``, each called exactly
    once regardless of row count) whose combined result the query then
    filters. The claim is in two parts: zero transcripts opened, ever; and the
    two batch calls stay at one each, whether the result is 3 rows or 33.

    **Not yet true, and said here rather than silently implied:** the digest
    batch itself is not narrowed by ``projects``/``pinned`` — only by
    ``owner`` — so it still reads every OWNED session's digest before the
    query discards the non-matching ones. A digest is far cheaper than a
    transcript (that is what made this facet safe to build on top of),
    but it is not free. Pushing those two facets into the digest query is
    the next tightening, not a claim this test makes today.
    """
    store_ = _CountingStore(root_dir=str(tmp_path / "sessions"))
    wanted = _seed_project_sessions(store_, "acme/beacon", 3)
    _seed_project_sessions(store_, "acme/ledger", noise)

    runtime = SessionRuntime(session_store=store_)
    store_.transcript_reads.clear()
    store_.calls.clear()
    rows = runtime.list_sessions(SessionQuery(projects=["acme/beacon"]))

    assert {str(row["session_id"]) for row in rows} == set(wanted)
    assert store_.transcript_reads == []
    assert store_.calls == {"list_session_digests": 1, "load_session_records": 1}


def test_an_unfiltered_listing_also_opens_no_transcript_and_batches_its_two_reads(tmp_path):
    """The control, updated the same way: cheap either way, batched either way.

    The original control asserted the UNFILTERED path was measurably more
    expensive than the filtered one — the contrast that gave the filtered
    test's zero-cost claim meaning. Now that neither path opens a transcript
    and both batch to one call each, the honest control is that both listings
    share the same low, row-count-independent cost — proving the earlier
    test's zero isn't just "an empty store would also pass this."
    """
    store_ = _CountingStore(root_dir=str(tmp_path / "sessions"))
    _seed_project_sessions(store_, "acme/beacon", 3)
    _seed_project_sessions(store_, "acme/ledger", 30)

    runtime = SessionRuntime(session_store=store_)
    store_.transcript_reads.clear()
    store_.calls.clear()
    rows = runtime.list_sessions(SessionQuery())

    assert len(rows) == 33
    assert store_.transcript_reads == []
    assert store_.calls == {"list_session_digests": 1, "load_session_records": 1}


def test_listing_never_writes_the_project_facet(tmp_path):
    """A read path must stay a read path — backfilling is the script's job.

    A listing that repaired the facet as it went would turn every page load into
    a write storm, and would make the backfill impossible to reason about.
    """
    store_ = SessionStore(root_dir=str(tmp_path / "sessions"))
    session_id = store_.create_session()
    _context(store_, session_id, {"project": "acme/beacon"})
    store_.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
    # Erase the facet the append hook wrote, leaving the transcript intact —
    # exactly the shape of a session that predates the facet.
    index = store_._load_index()
    index["projects"] = {}
    store_._save_index(index)

    runtime = SessionRuntime(session_store=store_)
    runtime.list_sessions(SessionQuery())
    runtime.list_sessions(SessionQuery(projects=["acme/beacon"]))

    assert store_.projects_for_session(session_id) == []


# ---------------------------------------------------------------------------
# 4b. Pinned ordering — and the filter a pin must never bypass
# ---------------------------------------------------------------------------


def _ordered_ids(rows: list[dict]) -> list[str]:
    """Project a listing down to its session ids, in listed order."""
    return [str(row["session_id"]) for row in rows]


@pytest.fixture()
def four_sessions(store):
    """Four listable sessions, oldest first, with distinct ``created_at``."""
    import time

    ids = []
    for index in range(4):
        session_id = store.create_session()
        store.append_event(session_id, {"type": "user", "payload": {"text": f"turn {index}"}})
        ids.append(session_id)
        time.sleep(0.005)
    return ids


def test_an_unpinned_listing_is_newest_first(store, four_sessions):
    """The baseline ordering the pinned pass must preserve within each group."""
    runtime = SessionRuntime(session_store=store)

    assert _ordered_ids(runtime.list_sessions()) == list(reversed(four_sessions))


def test_pinned_rows_come_first_and_the_rest_stay_newest_first(store, four_sessions):
    """Two stable sorts: pinned to the front, recency preserved behind them."""
    import time

    oldest, second, third, newest = four_sessions
    runtime = SessionRuntime(session_store=store)
    runtime.set_session_pinned(oldest, True)
    time.sleep(0.005)
    runtime.set_session_pinned(third, True)

    listed = _ordered_ids(runtime.list_sessions())

    assert set(listed[:2]) == {oldest, third}
    assert listed[2:] == [newest, second]


def test_within_the_pinned_group_ordering_is_by_PIN_recency(store, four_sessions):
    """Most-recently-pinned first — NOT most-recently-created first.

    Worth pinning down because it is the one place the two sorts do not compose
    the way "newest first, pinned to the top" suggests: the second pass sorts on
    ``pinned_at``, and pinned rows have distinct stamps, so it genuinely reorders
    them rather than merely moving the group. A user who pins an old session sees
    it above a newer pinned one, which is the intended affordance (a pin is an
    assertion made NOW) but is not what "stability preserves recency" implies.
    """
    import time

    oldest, _second, third, _newest = four_sessions
    runtime = SessionRuntime(session_store=store)
    runtime.set_session_pinned(third, True)
    time.sleep(0.005)
    runtime.set_session_pinned(oldest, True)

    listed = _ordered_ids(runtime.list_sessions())

    assert listed[:2] == [oldest, third]


def test_unpinning_returns_a_row_to_its_place_in_recency(store, four_sessions):
    """The ordering is derived from the stamp, so clearing it fully reverts."""
    runtime = SessionRuntime(session_store=store)
    oldest = four_sessions[0]
    runtime.set_session_pinned(oldest, True)
    assert _ordered_ids(runtime.list_sessions())[0] == oldest

    runtime.set_session_pinned(oldest, False)

    assert _ordered_ids(runtime.list_sessions()) == list(reversed(four_sessions))


def test_set_session_pinned_returns_the_resulting_stamp(store):
    """The runtime seam hands back what a surface renders and orders by."""
    runtime = SessionRuntime(session_store=store)
    session_id = store.create_session()

    stamp = runtime.set_session_pinned(session_id, True)
    assert isinstance(stamp, str) and stamp
    assert runtime.set_session_pinned(session_id, False) is None


def test_a_pinned_row_the_query_excludes_stays_excluded_by_project(store):
    """A pin is an ORDERING over what the query admitted — never a way past it.

    A pin that could hoist a row past a filter would be a leak: it would surface
    a session from another project (or another surface, or another owner) in a
    list that deliberately hides it.
    """
    runtime = SessionRuntime(session_store=store)
    wanted = _seed_project_sessions(store, "acme/beacon", 1)[0]
    hidden = _seed_project_sessions(store, "acme/ledger", 1)[0]
    runtime.set_session_pinned(hidden, True)

    listed = _ordered_ids(runtime.list_sessions(SessionQuery(projects=["acme/beacon"])))

    assert listed == [wanted]


def test_a_pinned_row_the_query_excludes_stays_excluded_by_owner(store):
    """Same law on the axis where the leak would cross a person, not a project."""
    runtime = SessionRuntime(session_store=store)
    mine = _with_user_turn(store, ALICE)
    theirs = _with_user_turn(store, BOB)
    runtime.set_session_pinned(theirs, True)

    listed = _ordered_ids(runtime.list_sessions(SessionQuery(owner=ALICE)))

    assert listed == [mine]


def test_a_pinned_archived_row_stays_hidden_by_default(store):
    """Archiving outranks pinning: the default listing hides it either way."""
    runtime = SessionRuntime(session_store=store)
    live = _with_user_turn(store)
    archived = _with_user_turn(store)
    store.archive_session(archived)
    runtime.set_session_pinned(archived, True)

    assert _ordered_ids(runtime.list_sessions()) == [live]
    assert archived in _ordered_ids(runtime.list_sessions(SessionQuery(include_archived=True)))


def test_a_page_is_cut_from_the_filtered_set_not_filtered_after_paging(store):
    """A facet filter narrows CANDIDATES, so a page is a page of matching rows.

    The two features arrived separately and compose in exactly one order. Paging
    first and filtering the page would make this return the pinned sessions
    among the newest 2 candidates — here, none of them — instead of the newest 2
    pinned sessions. The seed is deliberately adversarial: every pinned session
    is OLDER than every unpinned one, so a wrong order returns an empty page
    rather than a merely short one.
    """
    import time

    runtime = SessionRuntime(session_store=store)
    pinned = [_with_user_turn(store) for _ in range(3)]
    for session_id in pinned:
        runtime.set_session_pinned(session_id, True)
        time.sleep(0.005)
    for _ in range(5):
        _with_user_turn(store)
        time.sleep(0.005)

    page = _ordered_ids(runtime.list_sessions(SessionQuery(pinned=True), limit=2))

    assert len(page) == 2
    assert set(page) <= set(pinned)


def test_paging_a_project_filter_walks_only_that_project(store):
    """The same law on the project axis, across page boundaries.

    Concatenating the pages must reproduce the unpaginated filtered listing —
    a page that leaked a neighbouring project's session, or that dropped rows
    because filtering happened after the slice, fails here. The wanted project
    is seeded FIRST so every non-matching session is newer: under a paged-then-
    filtered order the first page would be entirely the other project's, and
    both assertions below would fail rather than only the ordering one.
    """
    import time

    runtime = SessionRuntime(session_store=store)
    wanted = []
    for _ in range(3):
        wanted += _seed_project_sessions(store, "acme/beacon", 1)
        time.sleep(0.005)
    for _ in range(4):
        _seed_project_sessions(store, "acme/ledger", 1)
        time.sleep(0.005)
    query = SessionQuery(projects=["acme/beacon"])

    full = _ordered_ids(runtime.list_sessions(query))
    paged = _ordered_ids(runtime.list_sessions(query, limit=2)) + _ordered_ids(
        runtime.list_sessions(query, limit=2, offset=2)
    )

    assert set(full) == set(wanted)
    assert paged == full


def test_both_drivers_page_the_filtered_set_identically(store, mongo_store):
    """Filter-then-page is a CONTRACT of the seam, not of one driver.

    The two implementations diverge sharply here — Mongo resolves the query into
    an indexed ``distinct`` and pages the ids before its expensive signal read,
    while the base template folds and pages the result — so the property has to
    be asserted against both or it only holds where it was written. Pinned rows
    are seeded oldest-first so a paged-then-filtered driver returns an empty
    page.
    """
    import time

    for driver in (store, mongo_store):
        pinned = []
        for _ in range(3):
            session_id = driver.create_session()
            driver.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
            driver.set_pinned(session_id, True)
            pinned.append(session_id)
            time.sleep(0.005)
        for _ in range(4):
            session_id = driver.create_session()
            driver.append_event(session_id, {"type": "user", "payload": {"text": "hi"}})
            time.sleep(0.005)

        page = driver.list_session_digests(SessionQuery(pinned=True), limit=2)

        assert len(page) == 2, f"{type(driver).__name__} paged the unfiltered set"
        assert {digest.session_id for digest in page} <= set(pinned)


def test_a_pin_survives_into_the_summary_only_when_set(store):
    """Both keys travel together, and an unpinned row carries neither.

    Absence rather than an explicit ``false`` is what keeps an unpinned row's
    summary byte-identical to what every existing client already parses.
    """
    runtime = SessionRuntime(session_store=store)
    session_id = _with_user_turn(store)

    unpinned = runtime.summarize_session(session_id)
    assert "pinned" not in unpinned
    assert "pinned_at" not in unpinned

    runtime.set_session_pinned(session_id, True)
    pinned = runtime.summarize_session(session_id)
    assert pinned["pinned"] is True
    assert pinned["pinned_at"] == store.get_pinned_at(session_id)
