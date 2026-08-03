#!/usr/bin/env python3
"""MongoDB-backed session storage driver."""

from __future__ import annotations

import os
import uuid

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from mewbo_core.common import get_logger
from mewbo_core.config import get_config_value
from mewbo_core.contracts.diff_stat import (
    DIFF_ENVELOPE_RE,
    DIFF_RESULT_KIND,
    EDIT_TOOL_ID_RE,
)
from mewbo_core.contracts.types import Event, EventRecord
from mewbo_core.session.event_cursor import EventCursor
from mewbo_core.session.session_digest import SessionDigest
from mewbo_core.session.session_provenance import CapabilityEvidence
from mewbo_core.session.session_query import SessionQuery
from mewbo_core.session.session_store import SessionRecord, SessionStoreBase, _utc_now

logging = get_logger(name="core.session_store_mongo")


class MongoSessionStore(SessionStoreBase):
    """MongoDB-backed storage for session transcripts and summaries.

    Uses three collections:

    - ``sessions``: session metadata (created_at, archived_at, summary).
    - ``events``: append-only event log indexed by ``(session_id, ts)``.
    - ``tags``: tag-name → session-id mapping.

    A local ``root_dir`` is still maintained for binary attachment file
    storage (uploaded via the API, read by ``ContextBuilder``).
    """

    def __init__(
        self,
        root_dir: str | None = None,
        *,
        uri: str | None = None,
        database: str | None = None,
    ) -> None:
        """Initialize MongoDB connection and local attachment directory."""
        super().__init__()
        # Local directory for attachment files.
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

        # MongoDB connection.
        if uri is None:
            uri = get_config_value("storage", "mongodb", "uri", default="mongodb://localhost:27017")
        if database is None:
            database = get_config_value("storage", "mongodb", "database", default="mewbo")

        self._client: MongoClient = MongoClient(
            uri, maxPoolSize=10, minPoolSize=2, serverSelectionTimeoutMS=5000
        )
        self._db: Database = self._client[database]

        # Fail fast: verify MongoDB is reachable before continuing.
        try:
            self._client.admin.command("ping")
        except Exception as exc:
            raise ConnectionError(
                f"MongoDB is unreachable at the configured URI. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc

        self._ensure_indexes()

    # -- helpers ------------------------------------------------------------

    def _col(self, name: str) -> Collection:
        """Return a MongoDB collection by name."""
        return self._db[name]

    def _ensure_indexes(self) -> None:
        """Create indexes idempotently on first connection.

        ``ix_events_session_ts`` serves the per-session reads — a transcript
        load AND its ``ts`` sort come out of one scan with no in-memory SORT
        stage — and, because ``session_id`` leads it, it also lets a ``$group``
        by ``session_id`` reach each session's FIRST event by DISTINCT_SCAN
        instead of touching every event (measured: 55 ms for 808 sessions).

        ``ix_events_type_session_ts`` serves the other half of
        :meth:`list_session_digests`: selecting the summary-relevant events
        across the whole collection is a range scan per type rather than a
        collection scan. Neither index makes a caller that asks for EVERYTHING
        fast — that is a query defect, not an index one — so add one here only
        after stating which query shape it serves.

        ``ix_sessions_projects``/``ix_sessions_pinned_at`` cover the predicates a
        filtered listing narrows on, and deliberately stop there: ``archived_at``
        and ``owner_subject`` are both overwhelmingly one value, so an index on
        either would be read past rather than used.
        """
        events = self._col("events")
        events.create_index(
            [("session_id", ASCENDING), ("ts", ASCENDING)],
            name="ix_events_session_ts",
            background=True,
        )
        events.create_index(
            [("type", ASCENDING), ("session_id", ASCENDING), ("ts", ASCENDING)],
            name="ix_events_type_session_ts",
            background=True,
        )
        # Multikey — one entry per element, so membership is an index seek
        # rather than a scan over every session's array.
        self._col("sessions").create_index(
            [("projects", ASCENDING)],
            name="ix_sessions_projects",
            background=True,
        )
        # Sparse: a pinned session is by nature a small minority of the record
        # set, so the index holds only the rows a pinned-only query wants and
        # the unpinned majority costs nothing to carry.
        self._col("sessions").create_index(
            [("pinned_at", DESCENDING)],
            name="ix_sessions_pinned_at",
            background=True,
            sparse=True,
        )

    # -- abstract implementations -------------------------------------------

    def create_session(self, owner: str | None = None) -> str:
        """Create a new session document and return its identifier."""
        session_id = uuid.uuid4().hex
        self.ensure_session(session_id, owner)
        return session_id

    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently materialise the ``sessions`` document for a known id.

        ``append_event`` only writes the ``events`` collection, so a session
        whose id was minted outside ``create_session`` (the realtime recorder)
        has events but no ``sessions`` document — invisible to ``list_sessions``,
        which reads the ``sessions`` collection. This upsert creates the record
        once. ``$setOnInsert`` means a replay never resets ``created_at`` or
        clears an ``archived_at`` already set on the existing doc (true no-op) —
        and it is what makes ``owner_subject`` set-once for free, so a second
        ``ensure_session`` can never re-point an existing session at a new owner.
        """
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$setOnInsert": {
                    "created_at": _utc_now(),
                    "archived_at": None,
                    "terminated_at": None,
                    "owner_subject": owner,
                    "summary": None,
                    "summary_updated_at": None,
                    "title": None,
                    "title_updated_at": None,
                }
            },
            upsert=True,
        )
        os.makedirs(os.path.join(self.root_dir, session_id), exist_ok=True)

    def session_dir(self, session_id: str) -> str:
        """Return the local attachment directory for a session."""
        path = os.path.join(self.root_dir, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _write_event(self, session_id: str, event: Event) -> None:
        """Insert an event document into the events collection, unconditionally."""
        record: EventRecord = self.stamp(event)
        self._col("events").insert_one({"session_id": session_id, **record})
        self._publish_appended(session_id, record)

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all events for a session, sorted by timestamp."""
        cursor = (
            self._col("events")
            .find({"session_id": session_id}, {"_id": 0, "session_id": 0})
            .sort("ts", ASCENDING)
        )
        return list(cursor)

    def load_recent_events(
        self,
        session_id: str,
        limit: int = 8,
        include_types: set[str] | None = None,
    ) -> list[EventRecord]:
        """Tail the events collection with a BOUNDED query.

        Overrides the base template method, which materialises the whole
        transcript before slicing — an O(transcript) read per call. A caller
        that only wants the last ``limit`` events (the startup run sweep) instead
        pays a single indexed range read: sort ``ts`` DESC + ``limit`` at the
        store, then reverse to restore ascending order. ``include_types`` filters
        server-side. Mirrors the targeted-query overrides below
        (``last_attestation_hash``/``tags_for_session``).
        """
        if limit <= 0:
            return []
        query: dict[str, object] = {"session_id": session_id}
        if include_types:
            query["type"] = {"$in": list(include_types)}
        cursor = (
            self._col("events")
            .find(query, {"_id": 0, "session_id": 0})
            .sort("ts", DESCENDING)
            .limit(limit)
        )
        events = list(cursor)
        events.reverse()
        return events

    def load_events_after(
        self, session_id: str, cursor: EventCursor | None
    ) -> list[EventRecord]:
        """Serve the cursor as an indexed RANGE read, not a filtered full read.

        ``O(matched events)``. Overrides the base, which streams the transcript
        and tests every event — a cursor that narrows the response while the
        read stays the record's whole history. ``ix_events_session_ts`` is
        compound on ``(session_id, ts)``, so a ``ts`` range within one session
        AND its sort come out of one index scan: measured on the largest live
        session (10,266 events), a cursor matching zero events went from 0.259 s
        to 0.001 s.

        The query bound is ``EventCursor.store_floor`` — deliberately COARSER
        than the cursor, because Mongo compares these timestamps as text while
        the cursor compares instants. The floor admits a superset (at most the
        events sharing one second with the cursor) and ``matches`` still decides,
        so the two comparisons cannot disagree about a boundary event. Pushing
        the exact value down instead would make a text comparison the
        authority.
        """
        query: dict[str, object] = {"session_id": session_id}
        if cursor is not None:
            query["ts"] = {"$gt": cursor.store_floor}
        cursor_docs = (
            self._col("events")
            .find(query, {"_id": 0, "session_id": 0})
            .sort("ts", ASCENDING)
        )
        events: list[EventRecord] = list(cursor_docs)
        if cursor is None:
            return events
        return [event for event in events if cursor.matches(event)]

    def latest_event_of_type(
        self, session_id: str, event_type: str, *, payload_key: str | None = None
    ) -> EventRecord | None:
        """Walk the index BACKWARDS and stop at the first match.

        ``O(1)``. Overrides the base template, which streams the whole
        transcript to keep one event. ``ix_events_type_session_ts`` leads on
        ``(type, session_id)``, so both filter terms are equality bounds and
        ``ts`` supplies the order — the plan is a backward index walk under a
        ``LIMIT``, and it examines exactly one key and one document however long
        the session is.

        Measured on the deployed store's largest session (10,296 events):
        ``explain("executionStats")`` reports ``stage=LIMIT``, ``nReturned=1``,
        ``totalKeysExamined=1``, ``totalDocsExamined=1``, against
        ``totalDocsExamined=10296`` for a full transcript read. No new index is
        owed.

        *payload_key* rides along as a residual filter on the fetched document,
        so it does not narrow the index bounds — the walk simply keeps stepping
        until a document also satisfies it. That leaves the cost bounded by the
        events of this TYPE rather than by the session (the busiest session on
        the deployed store holds 37 context events, and a divergent one resolved
        at ``totalDocsExamined=2``). The query restates
        :meth:`~SessionStoreBase.payload_key_is_set` in query language because
        it must; ``tests/test_latest_event_of_type.py`` drives one corpus
        through BOTH drivers so the two spellings cannot drift.
        """
        query: dict[str, object] = {"session_id": session_id, "type": event_type}
        if payload_key is not None:
            query[f"payload.{payload_key}"] = {"$exists": True, "$nin": [None, ""]}
        documents = list(
            self._col("events")
            .find(query, {"_id": 0, "session_id": 0})
            .sort("ts", DESCENDING)
            .limit(1)
        )
        return documents[0] if documents else None

    def session_digest(self, session_id: str) -> SessionDigest:
        """Project ONE session's digest instead of folding its transcript.

        ``O(one session's summary-relevant events)``. The single-session shape of
        :meth:`list_session_digests`, and what keeps the poll path from
        re-reading a whole transcript per tick to answer "what is this session's
        status". Both halves are served by ``ix_events_session_ts``: the first
        event is that index's first entry for the session, and the signal slice
        is a filtered read within it.
        """
        first = list(
            self._col("events").find({"session_id": session_id}).sort("ts", ASCENDING).limit(1)
        )
        relevant = self._signal_events({session_id}).get(session_id, [])
        return SessionDigest.from_parts(
            session_id,
            first_event=self._strip_keys(first[0]) if first else None,
            relevant=relevant,
        )

    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Delete all events with ``ts > cutoff_ts``."""
        result = self._col("events").delete_many(
            {"session_id": session_id, "ts": {"$gt": cutoff_ts}}
        )
        return result.deleted_count

    def save_summary(self, session_id: str, summary: str) -> None:
        """Upsert the summary field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$set": {
                    "summary": summary,
                    "summary_updated_at": _utc_now(),
                }
            },
            upsert=True,
        )

    def load_summary(self, session_id: str) -> str | None:
        """Load the summary field from the session document."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"summary": 1})
        if doc is None:
            return None
        return doc.get("summary")

    def save_title(self, session_id: str, title: str) -> None:
        """Upsert the title field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {
                "$set": {
                    "title": title,
                    "title_updated_at": _utc_now(),
                }
            },
            upsert=True,
        )

    def load_title(self, session_id: str) -> str | None:
        """Load the title field from the session document."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"title": 1})
        if doc is None:
            return None
        title = doc.get("title")
        return title if isinstance(title, str) and title else None

    def query_sessions(self, query: SessionQuery) -> list[str]:
        """Return sorted session IDs matching *query*, narrowed in the database.

        The whole filter is compiled by :meth:`SessionQuery.to_mongo` rather than
        assembled here, so the filesystem driver's predicate and this one can
        never mean different things.

        The ``owner_subject: None`` arm of that filter is doing double duty:
        Mongo equality to ``null`` also matches a MISSING field, so it selects
        both a session explicitly stamped unowned and one whose document
        carries no such field at all — the same property ``terminate_session``
        relies on. Without it every session lacking the field disappears from
        its own creator's list. ``archived_at``/``pinned_at`` inherit the
        same behaviour, which is what lets both be filtered on without a
        backfill.
        """
        ids = self._col("sessions").distinct("_id", query.to_mongo())
        return sorted(str(sid) for sid in ids)

    def set_pinned(self, session_id: str, pinned: bool) -> None:
        """Set or clear the ``pinned_at`` stamp on the session document.

        Unpinning ``$unset``s the field rather than writing ``None``, which is
        what keeps ``ix_sessions_pinned_at`` genuinely sparse: a sparse index
        omits documents MISSING the field, not documents holding null, so
        storing an explicit null for every unpinned session would put the whole
        collection back in the index it was made sparse to stay out of. It also
        keeps ``ensure_session`` free of a ``pinned_at`` default for the same
        reason — an unpinned session has no stamp at all.
        """
        update: dict[str, object] = (
            {"$set": {"pinned_at": _utc_now()}} if pinned else {"$unset": {"pinned_at": ""}}
        )
        self._col("sessions").update_one({"_id": session_id}, update, upsert=True)

    def get_pinned_at(self, session_id: str) -> str | None:
        """Return the stored ``pinned_at`` stamp, or ``None``."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"pinned_at": 1})
        if doc is None:
            return None
        stamp = doc.get("pinned_at")
        return stamp if isinstance(stamp, str) else None

    def record_project(self, session_id: str, project: str) -> None:
        """Add *project* to the document's ``projects`` set.

        ``$addToSet`` makes this idempotent in the DATABASE rather than by a
        read-then-write here, which matters because the common case is a
        re-emission of the same context on every turn and because two concurrent
        writers must not lose one another's project (the read-modify-write shape
        that bites ``WikiStoreBase.update_job``).
        """
        self._col("sessions").update_one(
            {"_id": session_id},
            {"$addToSet": {"projects": project}},
            upsert=True,
        )

    def projects_for_session(self, session_id: str) -> list[str]:
        """Return every project identity recorded on the session document."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"projects": 1})
        if doc is None:
            return []
        projects = doc.get("projects")
        return [str(p) for p in projects] if isinstance(projects, list) else []

    def list_session_digests(
        self,
        query: SessionQuery | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionDigest]:
        """Project one listing digest per session out of TWO queries.

        Overrides the base template, which folds every transcript. Cost here is
        ``O(collection)`` in rows plus ``O(summary-relevant events)`` in reads —
        the term that would otherwise dominate, and grow without bound, is the
        events a summary never looks at: ``llm_call_*``, ``agent_message_delta``,
        ``permission`` and the ~95 % of ``tool_result`` events that touched no
        file. Measured live: 138,512 events / 254.8 MB read to build 904 rows,
        against 6,576 events / 10.6 MB for the same rows.

        1. **Bounds** — ``$group`` by ``session_id`` taking ``$first`` after a
           ``(session_id, ts)`` sort. ``ix_events_session_ts`` turns that into a
           DISTINCT_SCAN, so it hops between sessions instead of reading their
           events. This exists only to keep ``created_at`` honest: a summary
           reads ``events[0]["ts"]``, and the first event is usually a type no
           listing would otherwise fetch.
        2. **Signal** — one filtered read for the events a summary folds. The
           filter is assembled by :meth:`_digest_match` from the predicates
           their owning classes publish, so it can only ever be WIDER than
           :meth:`SessionDigest.is_relevant`.

        Grouped in Python rather than with a second ``$group``: a ``$push``
        result is one BSON document and would inherit the 16 MB cap, which a
        single edit-heavy session could reach.

        A session matching *query* but carrying no events still yields a digest
        — an empty one — because whether it belongs on a listing is the caller's
        rule, not the store's.

        **When *limit* is given, this narrows the READ, not only the response.**
        Bounds are cheap regardless of collection size (measured: 14 ms for 809
        sessions via the DISTINCT_SCAN above), so they run for every candidate
        first, in ID order, and are sorted by ``ts`` descending — the same key
        :meth:`SessionRuntime.list_sessions` ends up sorting the finished
        summaries by, so a page here already lands in that order and predicts
        it exactly for every row that survives the caller's visibility filter
        (see the base template's docstring for why the prediction holds). ONLY
        the resulting page's ids are then handed to :meth:`_signal_events` — the
        expensive half. Measured live: unscoped, that query examines 45,707
        documents to return 6,578; scoped to a 50-session page, 2,416 to return
        289 — the query plan is unchanged (still index-served on both arms that
        can be), the CANDIDATE SET is what shrinks. ``limit=None`` (the default)
        takes the single-pass shape, so an unpaginated caller pays nothing for
        this branch existing.

        **The page is cut from what *query* already admitted, never before it.**
        Every predicate on :class:`SessionQuery` is answerable from the
        ``sessions`` document, so :meth:`query_sessions` applies all of them in
        the one indexed read that opens this method — which is what makes
        ``limit`` and a facet filter compose. Ordering them the other way would
        page the whole collection and filter the page afterwards, so
        ``pinned=True`` with a limit would return only the pinned sessions that
        happen to fall in the newest N — usually none.
        """
        query = query or SessionQuery(include_archived=True)
        session_ids = self.query_sessions(query)
        if not session_ids:
            return []
        # A query that admits every session needs no ``$in`` at all — passing
        # ``None`` drops the ``$match`` stage rather than restating the whole
        # collection as a literal id list.
        selects_all = query.is_unfiltered and query.include_archived
        if limit is None:
            wanted = None if selects_all else set(session_ids)
            first_events = self._first_events(wanted)
            signal = self._signal_events(wanted)
            return [
                SessionDigest.from_parts(
                    session_id,
                    first_event=first_events.get(session_id),
                    relevant=signal.get(session_id, []),
                )
                for session_id in session_ids
            ]
        first_events = self._first_events(set(session_ids))
        ordered = sorted(
            session_ids,
            key=lambda sid: str((first_events.get(sid) or {}).get("ts") or ""),
            reverse=True,
        )
        offset = max(offset, 0)
        page_ids = ordered[offset : offset + max(limit, 0)]
        signal = self._signal_events(set(page_ids)) if page_ids else {}
        return [
            SessionDigest.from_parts(
                session_id,
                first_event=first_events.get(session_id),
                relevant=signal.get(session_id, []),
            )
            for session_id in page_ids
        ]

    @staticmethod
    def _strip_keys(document: dict) -> EventRecord:
        """Return *document* as the event record shape ``load_transcript`` yields.

        Both digest queries select whole documents rather than a field list, so
        an event that grows a top-level key reaches a summary unchanged; only
        the two storage-owned keys are dropped. Naming the fields to KEEP would
        make the projection a third place that has to learn about a new key.
        """
        document.pop("_id", None)
        document.pop("session_id", None)
        return document

    def _first_events(self, session_ids: set[str] | None) -> dict[str, EventRecord]:
        """Return each session's earliest event, keyed by session id."""
        pipeline: list[dict[str, object]] = []
        if session_ids is not None:
            pipeline.append({"$match": {"session_id": {"$in": sorted(session_ids)}}})
        pipeline += [
            {"$sort": {"session_id": ASCENDING, "ts": ASCENDING}},
            {"$group": {"_id": "$session_id", "first": {"$first": "$$ROOT"}}},
        ]
        cursor = self._col("events").aggregate(pipeline, allowDiskUse=True)
        return {str(doc["_id"]): self._strip_keys(doc["first"]) for doc in cursor}

    def _signal_events(self, session_ids: set[str] | None) -> dict[str, list[EventRecord]]:
        """Return the summary-relevant events per session, in transcript order.

        Sorted in Python rather than by the server: the ``$or`` cannot be served
        by one index, so a server-side sort would be a blocking in-memory stage
        over the whole match, while the per-session lists here are small.

        The sort key breaks a ``ts`` tie on insertion order (``_id`` is
        monotonic within a client) instead of leaving it to scan order. Two
        events CAN share a timestamp — the clock has microsecond resolution and
        an append does not — and a summary that folds the last ``completion``
        wins-whole is one that must not see two terminals swap places.
        """
        query: dict[str, object] = {"$or": self._digest_match()}
        if session_ids is not None:
            query["session_id"] = {"$in": sorted(session_ids)}
        raw: dict[str, list[dict]] = {}
        for doc in self._col("events").find(query):
            raw.setdefault(str(doc.get("session_id") or ""), []).append(doc)
        return {
            session_id: [
                self._strip_keys(doc)
                for doc in sorted(docs, key=lambda d: (str(d.get("ts") or ""), d["_id"]))
            ]
            for session_id, docs in raw.items()
        }

    @staticmethod
    def _digest_match() -> list[dict[str, object]]:
        """Build the ``$or`` arms selecting every event a summary can fold.

        Each arm is the pushdown of one arm of
        :meth:`SessionDigest.is_relevant`, and every literal in it is READ from
        the class that owns the rule rather than restated: the signal types from
        ``SessionDigest``, the capability tool ids from ``CapabilityEvidence``,
        and the two diff gates from ``DiffStat``. A store-side filter is the
        classic place for a predicate to be copied and then drift, and the
        failure is silent in both directions — an arm that goes narrow drops a
        field off a row that still renders.

        The ``payload.result`` arm is the one that cannot be served by an index
        — an unanchored regex over a multi-KB string — so it is deliberately the
        TIGHT envelope gate rather than a bare "the result mentions a diff": the
        loose form matches 2,832 documents / 45.7 MB against 432 / 3.1 MB for the
        same envelopes, and the surplus is tools that merely READ diff-shaped
        bytes.

        **It is also the arm to reach for if this ever needs to be faster, and
        the measured price of keeping it is 0.38 s of a 1.15 s listing for 5
        documents out of 6,576.** It is kept because it is the only arm that
        does not assume a tool id: the envelope leg of
        ``DiffStat.from_tool_result`` has no tool-id gate, so every other arm
        together is NARROWER than the fold, and a store filter narrower than its
        fold drops a row's ``+N -M`` with nothing raised anywhere. Today only
        ``file_edit_tool`` and ``aider_edit_block_tool`` write an envelope and
        both match ``EDIT_TOOL_ID_RE`` — which is a fact about the current tree,
        not a property of the contract, and is exactly the kind of claim that has
        been wrong here before. Buying the time back means making the candidate
        set indexable (classifying at append time), not narrowing the predicate.
        """
        return [
            {"type": {"$in": sorted(SessionDigest.SIGNAL_TYPES)}},
            {
                "type": "tool_result",
                "payload.tool_id": {"$in": sorted(CapabilityEvidence.evidence_tool_ids())},
            },
            {
                "type": "tool_result",
                "payload.tool_id": {
                    "$regex": EDIT_TOOL_ID_RE.pattern,
                    "$options": "i",
                },
            },
            {"type": "tool_result", "payload.result": {"$regex": DIFF_ENVELOPE_RE.pattern}},
            {"type": "tool_result", "payload.result.kind": DIFF_RESULT_KIND},
        ]

    def get_owner(self, session_id: str) -> str | None:
        """Return the session's ``owner_subject``, or ``None`` if unowned."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"owner_subject": 1})
        if doc is None:
            return None
        subject = doc.get("owner_subject")
        return subject if isinstance(subject, str) else None

    def tag_session(self, session_id: str, tag: str) -> None:
        """Upsert a tag → session_id mapping in the tags collection."""
        self._col("tags").update_one(
            {"_id": tag},
            {"$set": {"session_id": session_id}},
            upsert=True,
        )

    def resolve_tag(self, tag: str) -> str | None:
        """Look up a tag and return the associated session ID."""
        doc = self._col("tags").find_one({"_id": tag})
        if doc is None:
            return None
        return doc.get("session_id")

    def list_tags(self) -> dict[str, str]:
        """Return all tag → session_id mappings."""
        return {
            doc["_id"]: doc["session_id"]
            for doc in self._col("tags").find({}, {"_id": 1, "session_id": 1})
        }

    def last_attestation_hash(self, session_id: str) -> str:
        """Targeted query over the events collection.

        Overrides the base reverse-scan so recovery issues one filtered/sorted
        lookup instead of loading the whole transcript — mirrors the
        ``tags_for_session`` override.
        """
        from mewbo_core.agents.attestation import GENESIS_HASH

        docs = list(
            self._col("events")
            .find({"session_id": session_id, "type": "attestation"})
            .sort("ts", DESCENDING)
            .limit(1)
        )
        if docs:
            payload = docs[0].get("payload")
            if isinstance(payload, dict):
                record_hash = payload.get("record_hash")
                if isinstance(record_hash, str) and record_hash:
                    return record_hash
        return GENESIS_HASH

    def tags_for_session(self, session_id: str) -> list[str]:
        """Tags pointing at a session via a targeted query.

        Overrides the base reverse-scan so ``list_sessions`` (which calls this
        per session) issues one filtered lookup instead of loading every tag.
        """
        return [
            str(doc["_id"])
            for doc in self._col("tags").find({"session_id": session_id}, {"_id": 1})
        ]

    def load_session_records(self, session_ids: list[str]) -> dict[str, SessionRecord]:
        """Batch-load session metadata in TWO queries, whatever the page size.

        Overrides the base default, which is correct but issues six round trips
        per session (``load_title`` / ``is_archived`` / ``get_terminated_at`` /
        ``get_owner`` / ``get_pinned_at`` / ``projects_for_session``). Listing a
        page of 900 sessions that way costs ~5,400 round trips to fetch six
        fields that all live on ONE document — a cost set by the number of rows
        and the network, not by how much data is involved. Here it is one
        ``$in`` over the primary key plus one read of the tag index.

        Every field is projected explicitly, so a session document that grows a
        large field later cannot silently start riding along on the listing
        path. An id with no document yields the default record — the same
        outcome the per-session getters produce for an unknown session, so the
        two implementations agree on the missing case rather than one raising.
        """
        if not session_ids:
            return {}
        docs = self._col("sessions").find(
            {"_id": {"$in": session_ids}},
            {
                "_id": 1,
                "title": 1,
                "archived_at": 1,
                "terminated_at": 1,
                "owner_subject": 1,
                "pinned_at": 1,
                "projects": 1,
            },
        )
        by_id = {str(doc["_id"]): doc for doc in docs}
        tags_by_session: dict[str, list[str]] = {}
        tag_docs = self._col("tags").find(
            {"session_id": {"$in": session_ids}}, {"_id": 1, "session_id": 1}
        )
        for doc in tag_docs:
            tags_by_session.setdefault(str(doc["session_id"]), []).append(str(doc["_id"]))
        records: dict[str, SessionRecord] = {}
        for session_id in session_ids:
            doc = by_id.get(session_id)
            if doc is None:
                records[session_id] = SessionRecord(tags=tags_by_session.get(session_id, []))
                continue
            title = doc.get("title")
            owner = doc.get("owner_subject")
            pinned_at = doc.get("pinned_at")
            projects = doc.get("projects")
            records[session_id] = SessionRecord(
                # Mirrors ``load_title``: an empty stored title is NOT a title,
                # so the summary falls back to the first user message.
                title=title if isinstance(title, str) and title else None,
                archived=doc.get("archived_at") is not None,
                terminated_at=doc.get("terminated_at"),
                owner=owner if isinstance(owner, str) else None,
                tags=tags_by_session.get(session_id, []),
                pinned_at=pinned_at if isinstance(pinned_at, str) else None,
                projects=[str(p) for p in projects] if isinstance(projects, list) else [],
            )
        return records

    def archive_session(self, session_id: str) -> None:
        """Set the archived_at timestamp on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {"$set": {"archived_at": _utc_now()}},
        )

    def unarchive_session(self, session_id: str) -> None:
        """Clear the archived_at field on the session document."""
        self._col("sessions").update_one(
            {"_id": session_id},
            {"$set": {"archived_at": None}},
        )

    def is_archived(self, session_id: str) -> bool:
        """Check whether the session has a non-null archived_at field."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"archived_at": 1})
        if doc is None:
            return False
        return doc.get("archived_at") is not None

    def terminate_session(self, session_id: str) -> bool:
        """Set ``terminated_at`` once, only while it is still unset.

        The ``terminated_at: None`` filter clause makes the write set-once —
        Mongo equality to ``null`` also matches a missing field, so a doc
        predating this field still qualifies, while a second call finds the
        stamp already present and no-ops (keeping the original time stable).
        ``modified_count`` is the arbitration signal: it is 1 only for the
        call whose filtered update actually flipped ``None`` -> a timestamp.
        """
        result = self._col("sessions").update_one(
            {"_id": session_id, "terminated_at": None},
            {"$set": {"terminated_at": _utc_now()}},
        )
        self._mark_terminated_cached(session_id)
        return result.modified_count > 0

    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the session's ``terminated_at`` timestamp, or ``None``."""
        doc = self._col("sessions").find_one({"_id": session_id}, {"terminated_at": 1})
        if doc is None:
            return None
        return doc.get("terminated_at")


__all__ = ["MongoSessionStore"]
