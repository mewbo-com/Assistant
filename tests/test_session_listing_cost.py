#!/usr/bin/env python3
"""A session listing must not cost a round trip per row, nor read every event.

`GET /api/sessions` renders one summary row per session, and two separate
unbounded costs are easy to incur doing it.

**Per row, a fan-out of round trips.** Every fact on a row that is NOT derived
from the transcript — title, archived, terminated, owner, tags — is stored on the
session record, so fetching each with its own store read costs five
reads per row means that on a networked driver the page cost ~5 round trips per
session for four fields living on a single document: a cost set by the row count
and the network, not by how much data is involved.

**Per row, the whole transcript.** The rest of a row IS folded from events, and
the listing loaded each session's full event log to fold it — so building the page
read every event ever recorded. Measured on the live store: 138,512 events /
254.8 MB to produce 904 rows, `O(all history)` wearing a listing's clothes. A
summary reads only a sparse slice of a transcript, so the store now projects that
slice (`SessionDigest`) and the fold stays exactly where it was.

Everything here asserts READ COUNTS and PROJECTION SIZES, never elapsed time. A
timing assertion on a shared machine measures the machine, passes on a fast one
and flakes on a loaded one — and it would go green for the wrong reason the
moment someone made the reads faster instead of fewer.

The counting store subclasses the REAL `SessionStore` and only tallies calls, so
these tests exercise production's own listing path rather than a mock of it.
"""

from __future__ import annotations

from unittest.mock import patch

import mongomock
import pytest
from mewbo_core.contracts.diff_stat import DIFF_RESULT_KIND, DiffStat
from mewbo_core.loop.session_runtime import SessionRuntime
from mewbo_core.session.session_digest import SessionDigest
from mewbo_core.session.session_query import SessionQuery
from mewbo_core.session.session_store import SessionRecord, SessionStore
from mewbo_core.session.session_store_mongo import MongoSessionStore


class _CountingStore(SessionStore):
    """Real JSON store that tallies how often each read primitive is called."""

    def __init__(self, root_dir: str) -> None:
        super().__init__(root_dir=root_dir)
        self.calls: dict[str, int] = {}
        #: Events the store handed the fold on the last listing — the number a
        #: transcript-loading listing would have made equal to the whole store.
        self.projected_events = 0

    def _tally(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def load_title(self, session_id: str) -> str | None:
        self._tally("load_title")
        return super().load_title(session_id)

    def is_archived(self, session_id: str) -> bool:
        self._tally("is_archived")
        return super().is_archived(session_id)

    def get_terminated_at(self, session_id: str) -> str | None:
        self._tally("get_terminated_at")
        return super().get_terminated_at(session_id)

    def get_owner(self, session_id: str) -> str | None:
        self._tally("get_owner")
        return super().get_owner(session_id)

    def list_tags(self) -> dict[str, str]:
        self._tally("list_tags")
        return super().list_tags()

    def tags_for_session(self, session_id: str) -> list[str]:
        self._tally("tags_for_session")
        return super().tags_for_session(session_id)

    def load_session_records(self, session_ids: list[str]) -> dict[str, SessionRecord]:
        self._tally("load_session_records")
        return super().load_session_records(session_ids)

    def load_transcript(self, session_id: str) -> list:
        self._tally("load_transcript")
        return super().load_transcript(session_id)

    def list_session_digests(
        self,
        query: SessionQuery | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionDigest]:
        self._tally("list_session_digests")
        digests = super().list_session_digests(query, limit=limit, offset=offset)
        self.projected_events = sum(len(digest.events) for digest in digests)
        return digests


def _seed(store: SessionStore, sessions: int, events_per_session: int) -> list[str]:
    """Create *sessions* sessions, each carrying *events_per_session* events."""
    ids: list[str] = []
    for index in range(sessions):
        session_id = store.create_session()
        store.append_user_turn(session_id, f"query {index}")
        for step in range(events_per_session):
            store.append_event(
                session_id,
                {
                    "type": "tool_result",
                    "payload": {
                        "tool_id": "read_file",
                        "operation": "get",
                        "tool_input": {"path": f"f{step}"},
                        "result": "ok",
                        "success": True,
                    },
                },
            )
        ids.append(session_id)
    return ids


class TestListingReadCost:
    def test_metadata_reads_do_not_grow_with_session_count(self, tmp_path):
        """The per-row metadata fan-out collapses to ONE batched read.

        The load is driven twice at different page sizes and the counts are
        compared against each other rather than against a hardcoded number: what
        must hold is that the metadata cost does not TRACK the row count, which
        a single-size assertion cannot distinguish from a coincidence.
        """
        small = _CountingStore(str(tmp_path / "small"))
        _seed(small, sessions=2, events_per_session=2)
        SessionRuntime(session_store=small).list_sessions()

        large = _CountingStore(str(tmp_path / "large"))
        _seed(large, sessions=20, events_per_session=2)
        SessionRuntime(session_store=large).list_sessions()

        # One batched call each, ten times the rows.
        assert small.calls.get("load_session_records") == 1
        assert large.calls.get("load_session_records") == 1
        # The per-session getters are never reached from the listing path at all.
        for probe in ("tags_for_session",):
            assert large.calls.get(probe) is None, f"{probe} still called per row"
        # The reverse tag index is resolved once for the page, not per row.
        assert large.calls.get("list_tags") == 1
        assert small.calls.get("list_tags") == 1

    def test_summarize_alone_still_reads_its_own_metadata(self, tmp_path):
        """A single-session summary is self-sufficient — no caller setup needed.

        The batching seam is an optimisation for a page, never a precondition:
        `summarize_session` called on its own must still resolve the same five
        facts itself, or every single-session caller silently loses its title,
        archived flag and owner.
        """
        store = _CountingStore(str(tmp_path))
        session_id = _seed(store, sessions=1, events_per_session=1)[0]
        store.save_title(session_id, "curated")
        store.tag_session(session_id, "wiki:demo")
        store.calls.clear()

        summary = SessionRuntime(session_store=store).summarize_session(session_id)

        assert store.calls.get("load_session_records") == 1
        assert summary["title"] == "curated"
        assert summary["archived"] is False
        assert summary["terminated"] is False


class TestListingSemanticsUnchanged:
    """The filter rules a row's presence depends on, pinned end to end."""

    def _runtime(self, tmp_path) -> tuple[SessionRuntime, SessionStore]:
        store = SessionStore(root_dir=str(tmp_path))
        return SessionRuntime(session_store=store), store

    def test_row_facts_match_a_direct_summarize(self, tmp_path):
        """A listed row is byte-identical to summarizing that session alone.

        The strongest available statement that batching changed nothing: the two
        paths now differ in how metadata is fetched, so they must still agree
        field for field. Compared as whole dicts rather than key by key — a
        per-key check silently ignores a key that one path stopped emitting.
        """
        runtime, store = self._runtime(tmp_path)
        first = store.create_session()
        store.append_user_turn(first, "hello there")
        store.tag_session(first, "wiki:proj")
        second = store.create_session()
        store.append_user_turn(second, "second turn")
        store.save_title(second, "pinned title")
        store.append_event(
            second,
            {
                "type": "completion",
                "payload": {"done": True, "done_reason": "completed", "task_result": "ok"},
            },
        )

        rows = {row["session_id"]: row for row in runtime.list_sessions()}
        for session_id in (first, second):
            assert rows[session_id] == runtime.summarize_session(session_id)

    def test_archived_hidden_unless_requested(self, tmp_path):
        runtime, store = self._runtime(tmp_path)
        kept = store.create_session()
        store.append_user_turn(kept, "visible")
        hidden = store.create_session()
        store.append_user_turn(hidden, "archived")
        store.archive_session(hidden)

        assert [r["session_id"] for r in runtime.list_sessions()] == [kept]
        with_archived = {
            r["session_id"]
            for r in runtime.list_sessions(SessionQuery(include_archived=True))
        }
        assert with_archived == {kept, hidden}

    def test_session_with_no_visible_event_is_skipped(self, tmp_path):
        """`session`/`context` events alone do not earn a row."""
        runtime, store = self._runtime(tmp_path)
        invisible = store.create_session()
        store.append_event(invisible, {"type": "session", "payload": {"event": "created"}})
        store.append_event(invisible, {"type": "context", "payload": {"model": "m"}})
        visible = store.create_session()
        store.append_user_turn(visible, "real turn")

        assert [r["session_id"] for r in runtime.list_sessions()] == [visible]

    def test_sorted_by_created_at_descending(self, tmp_path):
        runtime, store = self._runtime(tmp_path)
        ids = []
        for index in range(3):
            session_id = store.create_session()
            store.append_user_turn(session_id, f"turn {index}")
            ids.append(session_id)

        listed = [row["session_id"] for row in runtime.list_sessions()]
        assert listed == list(reversed(ids))

    def test_owner_narrowing_survives_batching(self, tmp_path):
        """`owner=` still narrows, and each row reports its own owner."""
        runtime, store = self._runtime(tmp_path)
        mine = store.create_session("subject:alice")
        store.append_user_turn(mine, "mine")
        unowned = store.create_session()
        store.append_user_turn(unowned, "unowned")
        theirs = store.create_session("subject:bob")
        store.append_user_turn(theirs, "theirs")

        rows = {
            r["session_id"]: r
            for r in runtime.list_sessions(SessionQuery(owner="subject:alice"))
        }
        assert set(rows) == {mine, unowned}
        assert rows[mine]["owner"] == "subject:alice"
        # An unowned session appends no ``owner`` key at all, so a deployment
        # without identity keeps a byte-identical summary.
        assert "owner" not in rows[unowned]


class TestLoadSessionRecords:
    """The batched primitive's own contract, driven on the real store."""

    def test_matches_the_per_session_getters(self, tmp_path):
        """The batch and the one-at-a-time reads must agree, field for field.

        This is the seam a second driver can silently diverge on: a store that
        answers the batch from a projection is free to spell a field
        differently from its own getter and nothing would notice, because both
        still return a plausible record.
        """
        store = SessionStore(root_dir=str(tmp_path))
        plain = store.create_session()
        titled = store.create_session("subject:owner")
        store.save_title(titled, "a title")
        store.tag_session(titled, "vcs:acme/beacon")
        archived = store.create_session()
        store.archive_session(archived)
        dead = store.create_session()
        store.terminate_session(dead)

        ids = [plain, titled, archived, dead]
        records = store.load_session_records(ids)
        for session_id in ids:
            record = records[session_id]
            assert record.title == store.load_title(session_id)
            assert record.archived == store.is_archived(session_id)
            assert record.terminated_at == store.get_terminated_at(session_id)
            assert record.owner == store.get_owner(session_id)
            assert sorted(record.tags) == sorted(store.tags_for_session(session_id))

    def test_unknown_id_yields_a_default_record(self, tmp_path):
        """An id with no record is present-and-empty, never missing.

        `list_sessions` indexes the result directly, so a dropped key would be a
        KeyError on the listing path rather than a missing row.
        """
        store = SessionStore(root_dir=str(tmp_path))
        records = store.load_session_records(["never-existed"])
        assert records["never-existed"] == SessionRecord()

    def test_empty_input_is_a_no_op(self, tmp_path):
        store = SessionStore(root_dir=str(tmp_path))
        assert store.load_session_records([]) == {}


@pytest.mark.parametrize("events_per_session", [2, 40])
def test_metadata_cost_is_independent_of_transcript_size(tmp_path, events_per_session):
    """Metadata reads are flat in transcript length as well as row count.

    Parameterised over a 20x difference in events per session: the batched
    metadata read must not grow with either dimension.
    """
    store = _CountingStore(str(tmp_path / f"n{events_per_session}"))
    _seed(store, sessions=5, events_per_session=events_per_session)
    SessionRuntime(session_store=store).list_sessions()

    assert store.calls.get("load_session_records") == 1
    assert store.calls.get("list_tags") == 1


# ---------------------------------------------------------------------------
# The other half: a listing must not read events a summary never folds.
# ---------------------------------------------------------------------------


def _noise(step: int) -> dict:
    """One event of the kind that dominates a real store and no row reads.

    ``llm_call_*``, ``agent_message_delta``, ``permission`` and read-only tool
    results were 84 % of the live store's events and 96 % of its bytes.
    """
    kinds = (
        {"type": "llm_call_start", "payload": {"model": "m", "step": step}},
        {"type": "llm_call_end", "payload": {"model": "m", "step": step}},
        {"type": "agent_message_delta", "payload": {"text": "x" * 64}},
        {"type": "permission", "payload": {"tool_id": "read_file", "granted": True}},
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "read_file",
                "tool_input": {"path": f"f{step}"},
                "result": "plain text, nothing edited",
                "success": True,
            },
        },
    )
    return kinds[step % len(kinds)]


def _seed_kitchen_sink(store: SessionStore, *, noise: int) -> str:
    """Seed ONE session exercising every field a summary row can carry.

    Deliberately not built with a loop of one event type: the projection is only
    meaningfully tested by a transcript that reaches every arm of the fold —
    title, origin, capabilities, diff arithmetic, the resilience facets, the
    terminal and the assertion that contradicts it — with enough surrounding
    noise that dropping a relevant event would be invisible in the count alone.
    """
    session_id = store.create_session()
    store.append_event(session_id, {"type": "session", "payload": {"event": "created"}})
    store.append_event(
        session_id,
        {
            "type": "context",
            "payload": {
                "client_capabilities": ["stlite", "apps"],
                "structured_workspace": "ws-42",
                "source_platform": "console",
            },
        },
    )
    for step in range(noise):
        store.append_event(session_id, _noise(step))
    store.append_user_turn(session_id, "please edit two files")
    # Leg 1 of the diff arithmetic: the envelope a file-edit tool writes.
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "file_edit_tool",
                "success": True,
                "result": (
                    '{"kind": "' + DIFF_RESULT_KIND + '", "additions": 9, '
                    '"deletions": 4, "text": "--- a\\n+++ b\\n"}'
                ),
            },
        },
    )
    # Leg 2: an external edit tool that emits no envelope at all.
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "Write",
                "success": True,
                "tool_input": {"file_path": "new.py", "content": "one\ntwo\n"},
                "result": "written",
            },
        },
    )
    # An envelope from a tool whose id says NOTHING about editing. The fold
    # counts it — the envelope leg has no tool-id gate — so a store filtering on
    # tool ids alone would drop it, and the row's `+N -M` would quietly shrink.
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {
                "tool_id": "mcp_forge_apply_change",
                "success": True,
                "result": '{"kind": "' + DIFF_RESULT_KIND + '", "additions": 7, "deletions": 3}',
            },
        },
    )
    # A capability earned by a successful gated call, and one by an artifact.
    store.append_event(
        session_id,
        {
            "type": "tool_result",
            "payload": {"tool_id": "submit_widget", "success": True, "result": "ok"},
        },
    )
    store.append_event(session_id, {"type": "widget_ready", "payload": {"widget_id": "w1"}})
    for step in range(noise):
        store.append_event(session_id, _noise(step + 3))
    store.append_event(
        session_id,
        {"type": "llm_retry", "payload": {"model": "model-a", "error_type": "timeout"}},
    )
    store.append_event(
        session_id,
        {
            "type": "llm_fallback",
            "payload": {"from_model": "model-a", "to_model": "model-b", "reason": "quota"},
        },
    )
    store.append_event(
        session_id,
        {"type": "completion", "payload": {"done": True, "done_reason": "completed"}},
    )
    store.append_event(
        session_id,
        {
            "type": "outcome_assertion",
            "payload": {"reason": "purpose_unmet", "detail": "no tests run"},
        },
    )
    return session_id


class TestListingReadsOnlyWhatItFolds:
    """The transcript half of the cost: reads must not track event volume."""

    def test_listing_never_loads_a_transcript(self, tmp_path):
        """The defect had ONE shape — `load_transcript` inside the row loop.

        Asserted as an absence rather than a bound, because any number greater
        than zero is the same defect: the primitive is `O(one session's events)`
        by contract, so calling it per row is what makes the page `O(all
        history)` however fast a single call happens to be.
        """
        store = _CountingStore(str(tmp_path))
        _seed(store, sessions=6, events_per_session=5)
        store.calls.clear()

        SessionRuntime(session_store=store).list_sessions()

        assert store.calls.get("load_transcript") is None
        assert store.calls.get("list_session_digests") == 1

    @pytest.mark.parametrize("noise", [1, 60])
    def test_projected_events_do_not_grow_with_transcript_length(self, tmp_path, noise):
        """Scale the axis claimed to be bounded, not the obviously bounded one.

        `noise` varies 60x with the session count and the relevant events held
        fixed, so the assertion isolates the dimension that actually grew
        without bound in production. Pinned to an exact count rather than a
        ceiling: an inequality passes just as happily when the projection quietly
        starts admitting a new event type it does not need.
        """
        store = _CountingStore(str(tmp_path / f"n{noise}"))
        _seed_kitchen_sink(store, noise=noise)

        SessionRuntime(session_store=store).list_sessions()

        # session, context, user, 4 tool_results, widget_ready, llm_retry,
        # llm_fallback, completion, outcome_assertion — and nothing else.
        assert store.projected_events == 12

    def test_a_read_only_tool_result_is_not_projected(self, tmp_path):
        """`tool_result` is the fattest type; only the few that matter are read.

        The single highest-value arm of the projection — 39,129 of the live
        store's events are tool results and 95 % of them touched no file and
        gated no capability.
        """
        store = SessionStore(root_dir=str(tmp_path))
        session_id = store.create_session()
        store.append_user_turn(session_id, "read something")
        for step in range(20):
            store.append_event(session_id, _noise(4))  # a read_file result

        digest = store.list_session_digests()[0]
        assert [event["type"] for event in digest.events] == ["user"]
        assert digest.session_id == session_id


class TestProjectionLosesNothing:
    """A projected row must equal the row a full transcript produces."""

    def test_row_matches_a_full_transcript_fold_field_for_field(self, tmp_path):
        """The load-bearing assertion for the whole change.

        The full transcript is passed EXPLICITLY. `summarize_session()` with no
        `events=` now folds the store's digest too — which is the point of the
        projection, and which would make the obvious spelling of this assertion
        compare a digest against itself and pass no matter what the projection
        dropped. Driving both inputs — projected slice and full log — through the
        one fold and demanding the same answer is the whole test.

        Compared as whole dicts: a key-by-key check silently ignores a key the
        projected path stopped emitting, which is exactly how a `diff_stat` or a
        `capabilities` chip would disappear.
        """
        store = SessionStore(root_dir=str(tmp_path))
        session_id = _seed_kitchen_sink(store, noise=8)
        runtime = SessionRuntime(session_store=store)

        row = runtime.list_sessions()[0]

        full = runtime.summarize_session(
            session_id, events=store.load_transcript(session_id)
        )
        assert row == full
        # And the default path (no `events=`) must agree with both, since every
        # single-session caller reaches the fold that way.
        assert runtime.summarize_session(session_id) == full
        # Named explicitly so a future reader can see the fold really was
        # exercised — an all-defaults row would satisfy the equality above.
        assert row["diff_stat"] == {"additions": 9 + 2 + 7, "deletions": 4 + 3}
        assert row["capabilities"] == ["stlite"]
        assert row["status"] == "unmet_goal"
        assert row["unmet_goal_reason"] == "purpose_unmet"
        assert row["failure_reason"] == "quota"
        assert row["models_tried"] == ["model-a", "model-b"]
        assert row["workspace"] == "ws-42"

    def test_running_session_with_no_visible_event_is_listed(self, tmp_path):
        """A live run earns a row before it has written anything to show.

        The one case where the `has_visible_event` filter must NOT fire, and the
        reason it is checked against `running` rather than on its own.
        """
        store = SessionStore(root_dir=str(tmp_path))
        runtime = SessionRuntime(session_store=store)
        live = store.create_session()
        store.append_event(live, {"type": "session", "payload": {"event": "created"}})
        idle = store.create_session()
        store.append_event(idle, {"type": "session", "payload": {"event": "created"}})
        runtime.is_running = lambda session_id: session_id == live

        rows = runtime.list_sessions()

        assert [row["session_id"] for row in rows] == [live]
        assert rows[0]["status"] == "running"

    def test_mongo_projection_agrees_with_the_folding_default(self, tmp_path):
        """Both drivers must produce the same rows from the same transcripts.

        `MongoSessionStore` pushes the projection down into a query, so its
        selection is a SECOND expression of `SessionDigest.is_relevant` — the
        classic place for a predicate to be copied and then drift narrower,
        which drops a field off a row that still renders. Driving one corpus
        through both drivers is the assertion that catches it; reading the query
        and reasoning about it is not.
        """
        json_store = SessionStore(root_dir=str(tmp_path / "json"))
        with patch(
            "mewbo_core.session.session_store_mongo.MongoClient", mongomock.MongoClient
        ):
            mongo_store = MongoSessionStore(
                root_dir=str(tmp_path / "mongo"),
                uri="mongodb://localhost:27017",
                database="test_listing",
            )
        for store in (json_store, mongo_store):
            _seed_kitchen_sink(store, noise=6)

        json_rows = SessionRuntime(session_store=json_store).list_sessions()
        mongo_rows = SessionRuntime(session_store=mongo_store).list_sessions()

        assert len(json_rows) == len(mongo_rows) == 1
        # Session ids and timestamps differ between the two stores by
        # construction; everything derived from the transcript must not.
        derived = ("title", "status", "capabilities", "diff_stat", "workspace",
                   "origin", "failure_reason", "models_tried", "unmet_goal_reason")
        for key in derived:
            assert mongo_rows[0].get(key) == json_rows[0].get(key), key

    def test_mongo_digest_carries_the_same_events_as_the_default(self, tmp_path):
        """Stated at the projection level too, not only through the summary.

        A summary can agree while the projections differ — several event types
        only move a field under conditions a single corpus may not reach. The
        digests themselves are the contract both drivers implement.
        """
        json_store = SessionStore(root_dir=str(tmp_path / "json"))
        with patch(
            "mewbo_core.session.session_store_mongo.MongoClient", mongomock.MongoClient
        ):
            mongo_store = MongoSessionStore(
                root_dir=str(tmp_path / "mongo"),
                uri="mongodb://localhost:27017",
                database="test_digest",
            )
        for store in (json_store, mongo_store):
            _seed_kitchen_sink(store, noise=6)

        json_types = [e["type"] for e in json_store.list_session_digests()[0].events]
        mongo_types = [e["type"] for e in mongo_store.list_session_digests()[0].events]
        assert mongo_types == json_types


class TestDiffCandidateGateIsNeverNarrowerThanTheFold:
    """`may_describe_edit` is what a store pushes down, so it must over-admit."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_id": "file_edit_tool", "success": True,
             "result": '{"kind": "diff", "additions": 3, "deletions": 1}'},
            {"tool_id": "Write", "success": True,
             "tool_input": {"file_path": "a.py", "content": "x\n"}},
            {"tool_id": "Edit", "success": True,
             "tool_input": {"file_path": "a.py", "old_string": "x\n", "new_string": "y\n"}},
            {"tool_id": "apply_patch", "success": True,
             "tool_input": {"file_path": "a.py", "content": "z\n"}},
            {"tool_id": "wiki_read_file", "success": True,
             "result": {"kind": "diff", "additions": 5, "deletions": 5}},
        ],
    )
    def test_admits_everything_the_fold_counts(self, payload):
        """Every payload worth non-zero lines must survive the gate.

        Asserted on the fold FIRST: a corpus entry that quietly stopped counting
        would otherwise make this pass vacuously, which is the failure mode a
        `if matches:` style guard has.
        """
        assert not DiffStat.from_tool_result(payload).is_empty
        assert DiffStat.may_describe_edit(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_id": "read_file", "result": "plain text"},
            {"tool_id": "Bash", "result": "diff --git a/x b/x"},
            {"tool_id": "web_url_read", "result": "an article about a diff"},
            {"tool_id": "spawn_agent", "result": '{"kind": "agent_result"}'},
        ],
    )
    def test_a_rejected_payload_could_never_have_counted(self, payload):
        """The gate's contract: rejecting implies the fold would find nothing.

        This is the property a store relies on when it filters at the query
        instead of in the fold. The reverse — admitting something worth zero —
        is allowed and costs only bytes.
        """
        assert not DiffStat.may_describe_edit(payload)
        assert DiffStat.from_tool_result(payload).is_empty


class _CountingMongoStore(MongoSessionStore):
    """Real `MongoSessionStore` (backed by mongomock) that tallies signal reads.

    `_signal_events` is the expensive half of `list_session_digests` — the one
    the projection docstring measures at 45,707 documents examined to return
    6,578, unscoped. Tallying what it actually RETURNS (not what a query
    planner examined, which mongomock does not model) is the read-count proxy
    available here; the doc-examined numbers are recorded from a real server in
    `session_store_mongo.py`'s own docstring instead.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.signal_docs_returned = 0

    def _signal_events(self, session_ids):
        result = super()._signal_events(session_ids)
        self.signal_docs_returned += sum(len(docs) for docs in result.values())
        return result


class TestPagination:
    """`limit`/`offset` must bound the QUERY, not merely the response.

    Naive pagination — fetch everything, slice the JSON — would shrink the
    payload while leaving server cost untouched: response size and server time
    are independent (see `apps/mewbo_console/CLAUDE.md`'s own warning about
    exactly this trap). The property pinned here is that a page's cost tracks
    the PAGE, never the collection it was drawn from.
    """

    def test_default_call_is_byte_identical_to_before_pagination_existed(self, tmp_path):
        store = SessionStore(root_dir=str(tmp_path))
        _seed(store, sessions=5, events_per_session=3)
        runtime = SessionRuntime(session_store=store)

        assert runtime.list_sessions() == runtime.list_sessions(limit=None, offset=0)

    def test_json_driver_page_is_the_unpaginated_prefix(self, tmp_path):
        """The file driver saves no reads under pagination, but the SLICE must
        still be correct — it pages the fold's result, not the fold itself."""
        store = SessionStore(root_dir=str(tmp_path))
        _seed(store, sessions=6, events_per_session=2)
        runtime = SessionRuntime(session_store=store)

        full = runtime.list_sessions()
        assert runtime.list_sessions(limit=3) == full[:3]
        assert runtime.list_sessions(limit=3, offset=3) == full[3:6]

    def test_mongo_scoped_read_does_not_grow_with_collection_size(self, tmp_path):
        """The load-bearing cost assertion — driven by comparison, not a magic number.

        Each seeded session contributes exactly one SIGNAL document (its own
        `user` turn) among many `read_file` `tool_result` noise events that
        match none of the digest's `$or` arms — the same shape the live store
        measured (signal events are a small, roughly session-proportional
        fraction of a transcript). An unpaginated listing must read one signal
        doc per session (`O(collection)`, the class a listing is allowed);
        a `limit=k` listing must read `k`, and that count must hold at THREE
        collection sizes — a single-size assertion cannot distinguish "bounded
        by the page" from "coincidentally 5 today".
        """
        counts_at_limit_5 = []
        for sessions in (10, 40, 120):
            with patch(
                "mewbo_core.session.session_store_mongo.MongoClient",
                mongomock.MongoClient,
            ):
                store = _CountingMongoStore(
                    root_dir=str(tmp_path / f"n{sessions}"),
                    uri="mongodb://localhost:27017",
                    database=f"test_pagination_{sessions}",
                )
            _seed(store, sessions=sessions, events_per_session=12)
            runtime = SessionRuntime(session_store=store)

            unpaginated_rows = runtime.list_sessions()
            assert len(unpaginated_rows) == sessions
            assert store.signal_docs_returned == sessions  # one `user` event each

            store.signal_docs_returned = 0
            page = runtime.list_sessions(limit=5)
            assert len(page) == 5
            counts_at_limit_5.append(store.signal_docs_returned)

        assert counts_at_limit_5 == [5, 5, 5]

    def test_mongo_page_matches_the_unpaginated_prefix_and_pages_never_overlap(
        self, tmp_path
    ):
        """Correctness alongside cost: the scoped read must not reorder or drop rows."""
        with patch(
            "mewbo_core.session.session_store_mongo.MongoClient", mongomock.MongoClient
        ):
            store = _CountingMongoStore(
                root_dir=str(tmp_path),
                uri="mongodb://localhost:27017",
                database="test_pagination_correctness",
            )
        _seed(store, sessions=10, events_per_session=2)
        runtime = SessionRuntime(session_store=store)

        full = runtime.list_sessions()
        page1 = runtime.list_sessions(limit=4, offset=0)
        page2 = runtime.list_sessions(limit=4, offset=4)

        assert page1 == full[:4]
        assert page2 == full[4:8]
        assert {r["session_id"] for r in page1}.isdisjoint(r["session_id"] for r in page2)

    def test_a_limit_covering_the_whole_collection_matches_the_unpaginated_set(
        self, tmp_path
    ):
        with patch(
            "mewbo_core.session.session_store_mongo.MongoClient", mongomock.MongoClient
        ):
            store = _CountingMongoStore(
                root_dir=str(tmp_path),
                uri="mongodb://localhost:27017",
                database="test_pagination_covering",
            )
        _seed(store, sessions=8, events_per_session=2)
        runtime = SessionRuntime(session_store=store)

        full = runtime.list_sessions()
        covering = runtime.list_sessions(limit=1000)

        assert {r["session_id"] for r in covering} == {r["session_id"] for r in full}
