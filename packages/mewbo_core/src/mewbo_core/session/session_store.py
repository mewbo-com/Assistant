#!/usr/bin/env python3
"""Session transcript storage and management.

Provides a ``SessionStoreBase`` ABC, a filesystem-backed ``SessionStore``
implementation, and a ``create_session_store()`` factory that returns the
configured driver (json or mongodb).
"""

from __future__ import annotations

import abc
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger, utc_now_iso as _utc_now
from mewbo_core.config import get_config_value
from mewbo_core.contracts.types import Event, EventRecord, UserPayload
from mewbo_core.session.event_cursor import EventCursor
from mewbo_core.session.session_digest import SessionDigest
from mewbo_core.session.session_query import SessionQuery
from mewbo_core.workspaces.project_identity import ProjectIdentity

if TYPE_CHECKING:
    from mewbo_core.session.compact import CompactionMode, CompactionResult

logging = get_logger(name="core.session_store")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """The per-session metadata a summary row needs, minus the transcript.

    Everything here is stored ON the session record rather than derived by
    folding events, which is exactly why it can be batch-loaded: a listing needs
    all of it for every row and none of it depends on reading a transcript.

    A plain frozen dataclass, not a Pydantic model: it never crosses a trust
    boundary — the store builds it from data it just read out of its own
    backend — so validating each field would buy nothing on a path whose entire
    purpose is to be cheap.

    ``tags`` is the reverse tag index for this session (the same list
    ``tags_for_session`` returns), carried here so a batch resolves the index
    once instead of per row.

    ``pinned_at``/``projects`` are the two filterable facets
    (``session/CLAUDE.md`` → "Filterable facets") and belong here for the same
    reason as every other field: both are stored ON the record rather than
    folded from events, so a caller reading them one session at a time is
    paying the exact per-row round-trip cost this class exists to collapse.
    """

    title: str | None = None
    archived: bool = False
    terminated_at: str | None = None
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    pinned_at: str | None = None
    projects: list[str] = field(default_factory=list)


class SessionStoreBase(abc.ABC):
    """Abstract interface for session storage backends.

    Each driver implements the 13 abstract storage primitives.  Higher-level
    operations (``fork_session``, ``load_recent_events``, ``compact_session``)
    are concrete template methods built on top of those primitives.
    """

    root_dir: str  # local directory — always present (used for attachment files)

    def __init__(self) -> None:
        """Initialise the in-memory termination cache shared by every backend.

        Populated by :meth:`_mark_terminated_cached` (called from each
        backend's ``terminate_session``) and consulted by
        :meth:`_guard_append`, so a hot-path append never pays a Mongo
        round-trip / index-file read to learn whether its own session was
        just terminated. Best-effort and per-process only: a session
        terminated by a DIFFERENT process/worker is still caught by the
        durable ``terminated_at`` stamp at every other termination-aware seam
        (``resolve_session``, recovery) — this cache only shortcuts the
        common same-process race between a live run and a concurrent
        ``/terminate`` call.
        """
        self._terminated_cache: set[str] = set()
        self._terminated_logged: set[str] = set()

    def _mark_terminated_cached(self, session_id: str) -> None:
        """Record *session_id* as terminated in the in-memory cache."""
        self._terminated_cache.add(session_id)

    def _guard_append(self, session_id: str) -> bool:
        """Return True iff *session_id* may still accept an appended event.

        Consults ONLY the in-memory cache (never a store round-trip) — see
        :meth:`__init__`. Structured-logs once per session (not once per
        dropped event) the first time an append is refused.
        """
        if session_id not in self._terminated_cache:
            return True
        if session_id not in self._terminated_logged:
            self._terminated_logged.add(session_id)
            logging.warning("Dropping event(s) appended to terminated session {}.", session_id)
        return False

    # -- abstract primitives ------------------------------------------------

    @abc.abstractmethod
    def create_session(self, owner: str | None = None) -> str:
        """Create a new session and return its identifier.

        *owner* is an OPAQUE subject string, never an identity object: core sits
        below the identity kernel in the dependency DAG and must not learn what a
        principal is. The caller that HAS one resolves it to a string first.
        ``None`` means unowned, which is what every session created without an
        authenticated caller is — see :meth:`list_sessions` for what that implies
        on the read side.
        """

    @abc.abstractmethod
    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently materialise a session RECORD for a known id.

        ``create_session`` mints its own uuid; some callers (the realtime
        write-behind recorder) need to back a session whose id was pre-minted
        elsewhere — e.g. for an in-flight Langfuse trace opened before any store
        write. Without a record, ``list_sessions`` (and every read surface built
        on it) never sees the id, so the transcript is an orphan: events exist,
        the session is invisible.

        This is the seam that closes that gap. It is idempotent — calling it on
        an existing session is a no-op (never resets created_at / archived state).
        ``create_session`` is implemented on top of it (mint id → materialise).

        The owner stamp is SET-ONCE at materialisation, like ``created_at`` and
        unlike ``archived_at``: a replay must not be able to re-point an existing
        session at a different subject, which would be an ownership takeover
        written through an idempotent no-op path.
        """

    @abc.abstractmethod
    def _write_event(self, session_id: str, event: Event) -> None:
        """Durably write *event*, unconditionally (no termination guard).

        The shared primitive :meth:`append_event` (guarded) and
        :meth:`append_terminal_event` (the termination tombstone's one
        exemption) both write through — the actual per-backend I/O
        (file append / Mongo insert) plus the ``_publish_appended`` fan-out.
        """

    def append_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript.

        No-ops (after a dropped-event log, once per session) once
        :meth:`terminate_session` has stamped this session terminated — see
        :meth:`_guard_append`. The ONE deliberate exception is
        :meth:`append_terminal_event`, used for the termination tombstone
        itself.
        """
        if not self._guard_append(session_id):
            return
        self._write_event(session_id, event)
        self._record_context_project(session_id, event)

    def _record_context_project(self, session_id: str, event: Event) -> None:
        """Accumulate the project a ``context`` event declares onto the record.

        This is the ONE seam that keeps a session's project set current, and it
        works because both writes that can bind a project already funnel through
        here: session creation writes a ``context`` event carrying ``project``,
        and ``ToolUseLoop.rebind_workspace`` writes a mid-run switch as a MERGED
        context event. So an auto-select session that moves between projects
        accumulates each one with no second write site to keep in step.

        Best-effort by construction: a facet that fails to record must never
        break the append it rode in on, so the failure is logged and swallowed
        rather than raised into the caller's path. The cost of losing one is a
        session missing from a project filter, not a lost transcript event.
        """
        if event.get("type") != "context":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        project = ProjectIdentity.from_context(payload)
        if project is None:
            return
        try:
            self.record_project(session_id, project)
        except Exception:
            logging.warning(
                "Failed to record project facet for session.", exc_info=True
            )

    def append_user_turn(
        self,
        session_id: str,
        text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """Append the ``user`` event that records ONE accepted turn.

        The single builder of that payload, because two seams write it: the
        acceptance seam (``SessionRuntime.start_async``, so the turn is durable
        before the executor's cold start) and the orchestration body itself (for
        every caller that reaches ``Orchestrator`` directly). A second
        hand-rolled ``{"type": "user", ...}`` literal in either place is free to
        drift from this one — silently, since both would still write a
        transcript event the readers accept.

        ``attachments`` is written only when non-empty, mirroring
        :class:`~mewbo_core.contracts.types.UserPayload`'s ``NotRequired`` key,
        so a turn without attachments carries no such key. It additively
        duplicates the sibling ``context`` event's
        descriptors so a client can render attachment cards above the user turn
        without joining across events.
        """
        payload: UserPayload = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        self.append_event(session_id, {"type": "user", "payload": payload})

    def append_terminal_event(self, session_id: str, event: Event) -> None:
        """Append an event exempt from the termination guard.

        ``SessionRuntime.terminate_session`` writes the ``session_terminated``
        tombstone through this seam immediately after the store's own
        ``terminate_session`` has already cached the session as terminated —
        an ordinary :meth:`append_event` call would otherwise drop its own
        closing event. No other caller should use this.
        """
        self._write_event(session_id, event)

    @abc.abstractmethod
    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all transcript events for a session. ``O(one session's events)``.

        A PER-SESSION read. It must never be called in a loop over sessions —
        that is ``O(all history)`` wearing a listing's clothes, and it is exactly
        the shape :meth:`list_session_digests` exists to replace.
        """

    @abc.abstractmethod
    def save_summary(self, session_id: str, summary: str) -> None:
        """Persist a summary for a session."""

    @abc.abstractmethod
    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary, if present."""

    @abc.abstractmethod
    def save_title(self, session_id: str, title: str) -> None:
        """Persist a display title for a session."""

    @abc.abstractmethod
    def load_title(self, session_id: str) -> str | None:
        """Load a previously saved title, if present."""

    @abc.abstractmethod
    def get_owner(self, session_id: str) -> str | None:
        """Return the owning subject, or ``None`` if the session is unowned.

        The read-through sibling of the stamp ``ensure_session`` writes, mirroring
        how ``is_archived`` reads ``archive_session``'s write.
        """

    @abc.abstractmethod
    def session_dir(self, session_id: str) -> str:
        """Return the local directory path for a session (used for attachments)."""

    @abc.abstractmethod
    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a tag with a session ID for quick lookup."""

    @abc.abstractmethod
    def resolve_tag(self, tag: str) -> str | None:
        """Resolve a tag to a session ID, if present."""

    @abc.abstractmethod
    def list_tags(self) -> dict[str, str]:
        """Return a mapping of tags to session IDs."""

    def tags_for_session(self, session_id: str) -> list[str]:
        """Return every tag pointing at ``session_id`` (reverse of ``resolve_tag``).

        Concrete default over ``list_tags`` so all backends share one
        implementation; the tag set is small. Provenance classification reads
        this to recover a session's origin (see ``session_provenance``).
        """
        return [tag for tag, sid in self.list_tags().items() if sid == session_id]

    def load_session_records(self, session_ids: list[str]) -> dict[str, SessionRecord]:
        """Batch-load the per-session metadata one summary row needs.

        A LISTING derives every row's title, archived flag, termination stamp,
        owner, tags, pin stamp and project set. Fetching them one session at a
        time is seven store reads per row — on a networked driver, seven ROUND
        TRIPS per row, so the cost of listing scales with the session count
        times a per-call latency that has nothing to do with how much data is
        involved. This is the seam that collapses them: one call for the whole
        page.

        This default is the CORRECT-everywhere implementation, not the fast one.
        It still reads per session, but it already removes the worst repetition
        by resolving the reverse tag index ONCE for the whole batch instead of
        rescanning it per row. A driver that can answer the whole batch in a
        single query overrides this (see ``MongoSessionStore``); a driver that
        cannot inherits behaviour identical to what the caller did by hand, so
        adding a driver can never silently produce a WRONG row — only a slower
        one.

        Unknown ids are returned with an empty record rather than omitted, so a
        caller can index the result without a membership test.
        """
        tags_by_session: dict[str, list[str]] = {}
        for tag, sid in self.list_tags().items():
            tags_by_session.setdefault(sid, []).append(tag)
        return {
            session_id: SessionRecord(
                title=self.load_title(session_id),
                archived=self.is_archived(session_id),
                terminated_at=self.get_terminated_at(session_id),
                owner=self.get_owner(session_id),
                tags=tags_by_session.get(session_id, []),
                pinned_at=self.get_pinned_at(session_id),
                projects=self.projects_for_session(session_id),
            )
            for session_id in session_ids
        }

    @abc.abstractmethod
    def set_pinned(self, session_id: str, pinned: bool) -> None:
        """Pin or unpin a session, stamping ``pinned_at`` on the record.

        Stored as a TIMESTAMP rather than a bool, matching ``archived_at`` and
        ``terminated_at``, because it doubles as the ordering key: a surface
        shows most-recently-pinned first without a second field. Unlike those
        two the stamp is REVERSIBLE by design — a pin is a user's assertion
        about their own list, not a lifecycle terminal.
        """

    @abc.abstractmethod
    def get_pinned_at(self, session_id: str) -> str | None:
        """Return the stored ``pinned_at`` stamp, or ``None`` if unpinned.

        The read-through sibling of :meth:`set_pinned`, mirroring how
        ``is_archived`` reads ``archive_session``'s write.
        """

    @abc.abstractmethod
    def record_project(self, session_id: str, project: str) -> None:
        """Add *project* to the set this session has worked in (idempotent).

        A SET, not a field, because a session can move: an auto-select session
        starts with no project and the agent may switch several times, and a
        filter has to find it under every one of them. Recording is additive and
        order-free, so a replayed context event is a no-op.
        """

    @abc.abstractmethod
    def projects_for_session(self, session_id: str) -> list[str]:
        """Return every project identity recorded for a session."""

    @abc.abstractmethod
    def query_sessions(self, query: SessionQuery) -> list[str]:
        """Return the session IDs matching *query*, narrowed at the STORE.

        The point of narrowing here rather than at the caller is that the caller
        builds each row by loading that session's whole transcript — so a
        session rejected by the query is one that is never opened. Every field of
        :class:`SessionQuery` is answerable from the session record alone,
        precisely so this can be a single indexed read.

        ``query.owner=None`` lists EVERYTHING — what a caller holding a
        read-all authority (or no identity at all) gets.
        A non-``None`` owner narrows to that subject's own sessions PLUS every
        UNOWNED one. **Unowned is not a hole in the filter, it is the migration
        semantic:** sessions predating the owner stamp carry no subject, and no
        subject can be reconstructed for them after the fact. Hiding them would
        make a user's existing work vanish from their own list, which is a worse
        failure than showing a pre-existing session to someone who could already
        list it before the stamp existed. The unowned set is closed and shrinking
        — every session created by an authenticated caller from here on is
        stamped — so this is a fading allowance, not a permanent widening.
        """

    def list_sessions(self, owner: str | None = None) -> list[str]:
        """List session IDs, optionally narrowed to what *owner* may see.

        Concrete delegate over :meth:`query_sessions`, kept because it is the
        established call shape. ``include_archived=True`` is the contract: this
        method returns archived sessions too, and the archived filter is applied
        by the caller.
        """
        return self.query_sessions(SessionQuery(owner=owner, include_archived=True))

    @abc.abstractmethod
    def archive_session(self, session_id: str) -> None:
        """Mark a session as archived."""

    @abc.abstractmethod
    def unarchive_session(self, session_id: str) -> None:
        """Remove archived status from a session."""

    @abc.abstractmethod
    def is_archived(self, session_id: str) -> bool:
        """Return True if a session is archived."""

    @abc.abstractmethod
    def terminate_session(self, session_id: str) -> bool:
        """Permanently mark a session terminated (irreversible).

        Stamps ``terminated_at`` **once** — a repeat call never moves the
        original timestamp. There is deliberately NO un-terminate primitive:
        termination is a one-way door. Mirrors
        ``archive_session`` but without the reverse operation.

        Returns ``True`` iff THIS call was the one that newly stamped the
        timestamp, ``False`` if the session was already terminated. This is
        the arbitration signal ``SessionRuntime.terminate_session`` reads to
        run its side effects (cancel + callbacks + event) exactly once even
        under concurrent callers — the store's set-once write is the only
        thing racing safely, so the runtime must never decide on its own
        unlocked read.
        """

    @abc.abstractmethod
    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the ISO ``terminated_at`` timestamp, or ``None`` if live.

        The read-through sibling of :meth:`terminate_session` (mirrors how
        ``is_archived`` reads ``archive_session``'s write).
        """

    def is_terminated(self, session_id: str) -> bool:
        """Return True iff a session was permanently terminated.

        Concrete over :meth:`get_terminated_at` so both backends share one
        implementation — the single derivation every termination guard reads.
        """
        return self.get_terminated_at(session_id) is not None

    @abc.abstractmethod
    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Delete all events with ``ts > cutoff_ts``.

        Returns the number of deleted events. Used by the recovery
        pipeline to clean up a failed run before re-driving.
        """

    # -- concrete template methods ------------------------------------------

    @staticmethod
    def stamp(event: Event) -> EventRecord:
        """Build the durable record for *event*, stamping or canonicalising ``ts``.

        The ONE place a stored timestamp is spelled, called by every backend's
        ``_write_event``. An event that carries no ``ts`` — everything the engine
        appends — gets the clock's. One that DOES is normalised to the same UTC
        spelling rather than stored verbatim: the mirror-ingest endpoint accepts
        a client's own timestamps, and a differing UTC offset would order
        differently as TEXT than as an instant. Every ``ts`` comparison in this
        package is textual (``load_transcript``'s sort, ``truncate_after``'s
        range, the cursor floor a store pushes down), so a single foreign
        spelling would sort a real event into the wrong place — silently, since
        text comparison never raises. The INSTANT is preserved; only its
        spelling is fixed.

        A value that cannot be parsed is kept exactly as sent. It is still
        evidence of what a client claimed, and replacing it with the clock would
        forge a timestamp for an event that already has one.
        """
        raw = event.get("ts")
        if raw is None:
            return {"ts": _utc_now(), **event}
        return {**event, "ts": EventCursor.canonical(raw) or raw}

    @staticmethod
    def _publish_appended(session_id: str, record: EventRecord) -> None:
        """Fan a just-appended record out to the process-wide event bus.

        The universal append choke-point: every backend calls this right after
        the durable write so SSE waiters wake immediately and ``on_event`` hooks
        fire — driven by the SAME persisted ``record`` ``load_transcript``
        returns, so a live SSE event is byte-identical to the backlog one.
        Best-effort: a bus failure must never break a durable append.
        """
        from mewbo_core.session.session_event_bus import get_session_event_bus

        try:
            get_session_event_bus().publish(session_id, record)
        except Exception:
            logging.warning("Session event bus publish failed.", exc_info=True)

    @staticmethod
    def merge_context_events(events: list[EventRecord]) -> dict[str, object]:
        """Reduce a transcript to its current context (most-recent payload wins).

        One reducer shared by :meth:`latest_context` (the trace-provenance path)
        and ``SessionRuntime.summarize_session`` (the origin/recovery path) so the
        rule for "what context is this session running under" lives in exactly one
        place. ``context`` events are sparse, so a full scan is cheap.
        """
        merged: dict[str, object] = {}
        for event in events:
            if event.get("type") == "context":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    merged.update(payload)
        return merged

    def latest_context(self, session_id: str) -> dict[str, object]:
        """Return the session's merged context (most-recent context event wins)."""
        return self.merge_context_events(self.load_transcript(session_id))

    def last_attestation_hash(self, session_id: str) -> str:
        """Return the attestation chain's head to re-seed on recovery.

        Concrete default over :meth:`load_transcript`: scans for the most
        recent ``type == "attestation"`` event and returns its persisted
        ``record_hash``, else the chain's genesis hash — mirrors the
        ``tags_for_session`` concrete-default idiom so every backend shares
        one scan; ``MongoSessionStore`` overrides with a targeted query.
        """
        from mewbo_core.agents.attestation import GENESIS_HASH

        for event in reversed(self.load_transcript(session_id)):
            if event.get("type") != "attestation":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                record_hash = payload.get("record_hash")
                if isinstance(record_hash, str) and record_hash:
                    return record_hash
        return GENESIS_HASH

    def fork_session(self, source_session_id: str, owner: str | None = None) -> str:
        """Create a new session by copying events, summary, and title from another.

        The fork is stamped for whoever asked for it, NOT for the source's owner:
        a fork is a new session that happens to start with a copy of a transcript.
        Leaving it unstamped would be worse than either — an unowned fork of an
        owned session is visible to every lister, so forking would launder a
        session out of its owner's scope.
        """
        events = self.load_transcript(source_session_id)
        summary = self.load_summary(source_session_id)
        title = self.load_title(source_session_id)
        new_session_id = self.create_session(owner)
        for event in events:
            self.append_event(new_session_id, event)
        if summary:
            self.save_summary(new_session_id, summary)
        if title:
            self.save_title(new_session_id, title)
        return new_session_id

    def fork_session_at(
        self, source_session_id: str, cutoff_ts: str, owner: str | None = None
    ) -> str:
        """Fork a session, keeping only events with ``ts <= cutoff_ts``.

        Composes :meth:`fork_session` + :meth:`truncate_after` and clears the
        copied summary (which may reference events beyond the cutoff).
        """
        new_id = self.fork_session(source_session_id, owner)
        self.truncate_after(new_id, cutoff_ts)
        self.save_summary(new_id, "")
        return new_id

    def list_session_digests(
        self,
        query: SessionQuery | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionDigest]:
        """Return one listing digest per session matching *query*.

        The seam a LISTING reads instead of calling :meth:`load_transcript` per
        id. One digest per session, in the order :meth:`query_sessions` returns
        them when *limit* is ``None``; a session with no events yields an empty
        digest rather than being dropped, because whether it belongs on a
        listing is the caller's rule (a running session has no events until its
        first append). ``None`` means an unnarrowed call — every session,
        archived included — leaving a caller that filters for itself to do so.

        **Cost is per DRIVER, and the base template is the expensive one.**
        This default folds every transcript, so it is ``O(all history)`` —
        honest, correct, and the reason ``MongoSessionStore`` overrides it with
        a projection that is ``O(collection)`` in rows plus ``O(summary-relevant
        events)`` in reads. A backend that gains an index owes an override; a
        backend that cannot must say so here rather than let a caller assume the
        listing is cheap.

        What the default still buys: the fold STREAMS, so peak memory is the
        RELEVANT events rather than the whole transcript — a 10,266-event
        session never has to be resident to contribute one row.

        *limit*/*offset* page the RESULT of the fold this driver already pays
        for — the file backend has no cheap way to learn a session's first
        timestamp without opening it, so paging here narrows what is RETURNED,
        not what is READ. ``MongoSessionStore`` overrides this to narrow the
        read too; see its docstring for why that split is real. Sort key is
        each digest's first event's ``ts`` (== the eventual ``created_at`` for
        every row that survives the caller's own visibility filter — see
        ``SessionRuntime.list_sessions``), descending, so a page here orders the
        same way the final listing does.

        **The page is cut from what *query* already admitted.** Even on a driver
        where paging cannot narrow the read, the two must compose in that order:
        filtering a page instead of paging the filtered set would make
        ``pinned=True`` with a ``limit`` return only the pinned sessions inside
        the newest N candidates, which is usually none of them.
        """
        digests = [
            SessionDigest.from_events(session_id, self.stream_transcript(session_id))
            for session_id in self.query_sessions(
                query or SessionQuery(include_archived=True)
            )
        ]
        if limit is None:
            return digests
        digests.sort(
            key=lambda digest: str(digest.events[0].get("ts")) if digest.events else "",
            reverse=True,
        )
        offset = max(offset, 0)
        return digests[offset : offset + max(limit, 0)]

    def session_digest(self, session_id: str) -> SessionDigest:
        """Return ONE session's listing digest — the single-session sibling above.

        ``O(one session's summary-relevant events)`` for a driver that projects,
        ``O(one session's events)`` for one that folds. Exists because a
        summary is not only a listing concern: the poll path re-derives status
        for a single session on every tick, and doing that from the full
        transcript is the same defect at a different scale — 0.211 s per poll on
        the largest live session, once a second, per open client.

        Deliberately NOT expressed as ``list_session_digests`` narrowed to one
        id: that route pays the listing's own setup (enumerating sessions) to
        answer a question about one.
        """
        return SessionDigest.from_events(session_id, self.stream_transcript(session_id))

    def load_events_after(
        self, session_id: str, cursor: EventCursor | None
    ) -> list[EventRecord]:
        """Return the events strictly newer than *cursor* (all of them if ``None``).

        ``O(one session's events)`` here, and that is the point of the seam
        rather than an acceptance of it: the base has no index to narrow with,
        so it streams and tests, but a driver that CAN turn the cursor into a
        range read overrides this and becomes ``O(matched events)``. Filtering a
        fully-materialised transcript in Python is a cursor in name only — the
        response shrinks and the work does not.

        A ``None`` cursor means "everything". It never means "the cursor was
        bad": an unparseable value is refused at the boundary that received it,
        because a filter that cannot be applied must not widen to the whole
        collection.
        """
        events = self.stream_transcript(session_id)
        if cursor is None:
            return list(events)
        return [event for event in events if cursor.matches(event)]

    def stream_transcript(self, session_id: str) -> Iterator[EventRecord]:
        """Yield a session's events in ts order without materialising them.

        ``O(one session's events)`` in time, ``O(1)`` in memory for a driver that
        overrides it. The default just iterates :meth:`load_transcript`, so it
        buys nothing on its own — it exists so :meth:`list_session_digests` has
        one seam a line-oriented backend can make genuinely streaming.
        """
        yield from self.load_transcript(session_id)

    def load_recent_events(
        self,
        session_id: str,
        limit: int = 8,
        include_types: set[str] | None = None,
    ) -> list[EventRecord]:
        """Load the most recent events, optionally filtered by type."""
        events = self.load_transcript(session_id)
        if include_types:
            events = [event for event in events if event.get("type") in include_types]
        if limit <= 0:
            return []
        return events[-limit:]

    @staticmethod
    def payload_key_is_set(event: EventRecord, key: str) -> bool:
        """Is *key* present on this event's payload with a value worth reading?

        The ONE spelling of the narrowing :meth:`latest_event_of_type` applies
        for *payload_key*, published here so ``MongoSessionStore`` can push an
        equivalent query down rather than restate the rule. Present, not
        ``None``, not the empty string — deliberately no further judgement: what
        counts as a USABLE value belongs to the caller that knows what the key
        means, and a store that guessed would silently skip an event a caller
        would have accepted.
        """
        payload = event.get("payload")
        if not isinstance(payload, dict) or key not in payload:
            return False
        value = payload[key]
        return value is not None and value != ""

    def latest_event_of_type(
        self, session_id: str, event_type: str, *, payload_key: str | None = None
    ) -> EventRecord | None:
        """Return the NEWEST event of *event_type*, or ``None`` when there is none.

        ``O(one session's events)`` here and ``O(1)`` on a driver that can walk
        an index backwards (``MongoSessionStore`` overrides). Same honest split
        as :meth:`load_events_after` and :meth:`load_recent_events`: the base
        has no cheap reverse read, so it streams and keeps the last match — a
        caller must not read this primitive as cheap on every backend. Memory
        stays ``O(1)``: it holds one event, never the transcript.

        **Bounded by the TYPE, never by a count.** Tailing the last N events is
        the tempting cheap answer and it is a wrong one — the newest event of a
        sparse type sits arbitrarily far back in a session with a long run since
        the last one, so a window that misses it reports "no such event". That
        is a wrong ANSWER, not a slow one, and a caller turns it into a refusal.

        *payload_key* narrows further, to events whose payload satisfies
        :meth:`payload_key_is_set`. It exists because "the newest event of this
        type" and "the newest event of this type that CARRIES the field I came
        for" are different questions, and a caller answering the second with the
        first is wrong whenever a later event of the same type omits the field.
        Context events are exactly that shape: they are merged key-by-key
        (:meth:`merge_context_events`), so a session can write a ``project``
        and then several context events that say nothing about one. Measured on
        the deployed store, 15 of the 204 sessions carrying a ``project``
        anywhere in context had a NEWER context event without it.

        Returns the EVENT, not a field off it, so a second caller wanting a
        different key out of the same event is served by the same read.
        """
        latest: EventRecord | None = None
        for event in self.stream_transcript(session_id):
            if event.get("type") != event_type:
                continue
            if payload_key is not None and not self.payload_key_is_set(event, payload_key):
                continue
            latest = event
        return latest

    async def compact_session(
        self,
        session_id: str,
        mode: CompactionMode | None = None,
        **kwargs: Any,
    ) -> CompactionResult:
        """Compact a session's transcript using structured summarization."""
        from mewbo_core.session.compact import (
            CompactionMode as CM,
            CompactionResult as CR,
            compact_conversation,
        )

        resolved_mode: CM = CM(mode) if mode is not None else CM.PARTIAL
        events = self.load_transcript(session_id)
        result: CR = await compact_conversation(events, resolved_mode, **kwargs)
        self.save_summary(session_id, result.summary)
        return result


# ---------------------------------------------------------------------------
# Filesystem (JSON) driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPaths:
    """Resolved filesystem paths for a session."""

    root: str
    session_id: str

    @property
    def session_dir(self) -> str:
        """Directory for session artifacts."""
        return os.path.join(self.root, self.session_id)

    @property
    def transcript_path(self) -> str:
        """Path to the JSONL transcript file."""
        return os.path.join(self.session_dir, "transcript.jsonl")

    @property
    def summary_path(self) -> str:
        """Path to the summary JSON file."""
        return os.path.join(self.session_dir, "summary.json")

    @property
    def title_path(self) -> str:
        """Path to the title JSON file."""
        return os.path.join(self.session_dir, "title.json")


class SessionStore(SessionStoreBase):
    """Filesystem-backed storage for session transcripts and summaries."""

    def __init__(self, root_dir: str | None = None) -> None:
        """Initialize the store and ensure the root directory exists."""
        super().__init__()
        if root_dir is None:
            root_dir = get_config_value("runtime", "session_dir", default="./data/sessions")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)

    def _index_path(self) -> str:
        """Return the path for the session index file."""
        return os.path.join(self.root_dir, "index.json")

    def _load_index(self) -> dict[str, dict[str, Any]]:
        """Load the session index from disk or return defaults.

        Bucket values are ``Any`` rather than ``str`` because ``projects`` maps a
        session to a LIST of identities while every other bucket maps it to a
        single stamp. A bucket missing from an index written before it existed
        reads as absent and is defaulted at each use site, so no migration is
        needed to start reading one.
        """
        index_path = self._index_path()
        if not os.path.exists(index_path):
            return {
                "tags": {},
                "archived": {},
                "terminated": {},
                "owners": {},
                "pinned": {},
                "projects": {},
            }
        with open(index_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save_index(self, data: dict[str, dict[str, Any]]) -> None:
        """Persist the session index to disk."""
        with open(self._index_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def create_session(self, owner: str | None = None) -> str:
        """Create a new session directory and return its identifier."""
        session_id = uuid.uuid4().hex
        self.ensure_session(session_id, owner)
        return session_id

    def ensure_session(self, session_id: str, owner: str | None = None) -> None:
        """Idempotently create the session directory for a known id.

        For the filesystem driver a session "record" IS its directory (that's
        what :meth:`list_sessions` enumerates), so materialisation is a
        ``makedirs`` — ``exist_ok=True`` makes it a safe no-op on replay.

        The owner lands in its own index bucket beside ``archived``/``terminated``
        rather than in a per-session file, so the list filter costs one index read
        instead of one stat per session.

        The stamp is written ONLY when this call is the one that materialised the
        record — the filesystem analogue of the Mongo driver's ``$setOnInsert``,
        and the reason the directory's prior existence is checked before creating
        it. Stamping on any call would make an ALREADY-UNOWNED session claimable
        by whoever touched it next, and since an unowned session is visible to
        every lister, that is not necessarily its creator. An unowned call never
        writes at all, so a deployment with no identity configured leaves the
        index exactly as it was.
        """
        paths = self._paths(session_id)
        already_materialised = os.path.isdir(paths.session_dir)
        os.makedirs(paths.session_dir, exist_ok=True)
        if owner is None or already_materialised:
            return
        index = self._load_index()
        index.setdefault("owners", {})[session_id] = owner
        self._save_index(index)

    def get_owner(self, session_id: str) -> str | None:
        """Return the stamped owning subject, or ``None`` if unowned."""
        return self._load_index().get("owners", {}).get(session_id)

    def _paths(self, session_id: str) -> SessionPaths:
        """Build filesystem paths for a session."""
        return SessionPaths(root=self.root_dir, session_id=session_id)

    def session_dir(self, session_id: str) -> str:
        """Return the directory path for a session."""
        return self._paths(session_id).session_dir

    def _write_event(self, session_id: str, event: Event) -> None:
        """Append a single event record to the session transcript, unconditionally."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        payload: EventRecord = self.stamp(event)
        with open(paths.transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        self._publish_appended(session_id, payload)

    def load_transcript(self, session_id: str) -> list[EventRecord]:
        """Load all transcript events for a session. ``O(one session's events)``."""
        return list(self.stream_transcript(session_id))

    def stream_transcript(self, session_id: str) -> Iterator[EventRecord]:
        """Yield transcript events one parsed line at a time.

        ``O(1)`` in memory, which is what makes the base
        :meth:`~SessionStoreBase.list_session_digests` template usable here: a
        listing folds each session's file as it reads and retains only the
        relevant events, instead of holding a whole transcript to produce one
        row. The PARSE still costs ``O(one session's events)`` — a JSONL file
        carries no index, so there is no honest way to find a session's
        ``completion`` event without reading past everything before it, and this
        driver's listing therefore stays ``O(all history)`` in CPU. That is the
        floor for the default driver, and the reason MongoDB is the recommended
        backend for a store that has accumulated real history.
        """
        paths = self._paths(session_id)
        if not os.path.exists(paths.transcript_path):
            return
        with open(paths.transcript_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("Skipping malformed transcript line.")

    def truncate_after(self, session_id: str, cutoff_ts: str) -> int:
        """Rewrite the transcript keeping only events with ``ts <= cutoff_ts``."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.transcript_path):
            return 0
        events = self.load_transcript(session_id)
        kept = [e for e in events if e.get("ts", "") <= cutoff_ts]
        removed = len(events) - len(kept)
        if removed:
            with open(paths.transcript_path, "w", encoding="utf-8") as handle:
                for event in kept:
                    handle.write(json.dumps(event) + "\n")
        return removed

    def save_summary(self, session_id: str, summary: str) -> None:
        """Persist a summary for a session."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        with open(paths.summary_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "updated_at": _utc_now()}, handle, indent=2)

    def load_summary(self, session_id: str) -> str | None:
        """Load a previously saved summary, if present."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.summary_path):
            return None
        with open(paths.summary_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("summary")

    def save_title(self, session_id: str, title: str) -> None:
        """Persist a display title for a session."""
        paths = self._paths(session_id)
        os.makedirs(paths.session_dir, exist_ok=True)
        with open(paths.title_path, "w", encoding="utf-8") as handle:
            json.dump({"title": title, "updated_at": _utc_now()}, handle, indent=2)

    def load_title(self, session_id: str) -> str | None:
        """Load a previously saved title, if present."""
        paths = self._paths(session_id)
        if not os.path.exists(paths.title_path):
            return None
        with open(paths.title_path, encoding="utf-8") as handle:
            data = json.load(handle)
        title = data.get("title")
        return title if isinstance(title, str) and title else None

    def query_sessions(self, query: SessionQuery) -> list[str]:
        """List session IDs in the root directory, narrowed by *query*.

        Reads the index ONCE for the whole listing rather than once per
        predicate per session — the filesystem driver's every index-backed fact
        is a full read-and-parse of ``index.json``, so a per-session lookup would
        turn one file read into four times the session count.
        """
        if not os.path.exists(self.root_dir):
            return []
        session_ids = sorted(
            name
            for name in os.listdir(self.root_dir)
            if os.path.isdir(os.path.join(self.root_dir, name))
        )
        if query.is_unfiltered and query.include_archived:
            return session_ids
        index = self._load_index()
        owners = index.get("owners", {})
        archived = index.get("archived", {})
        pinned = index.get("pinned", {})
        projects = index.get("projects", {})
        return [
            sid
            for sid in session_ids
            if query.matches_record(
                # An id absent from the owners bucket is UNOWNED and stays
                # visible — see the base's contract for why that is the
                # migration semantic rather than a hole.
                owner=owners.get(sid),
                archived=sid in archived,
                pinned=sid in pinned,
                projects=list(projects.get(sid, [])),
            )
        ]

    def set_pinned(self, session_id: str, pinned: bool) -> None:
        """Stamp or clear ``pinned`` for a session in the index."""
        index = self._load_index()
        bucket = index.setdefault("pinned", {})
        if pinned:
            bucket[session_id] = _utc_now()
        elif session_id not in bucket:
            return  # Nothing to clear — don't rewrite the file for a no-op.
        else:
            bucket.pop(session_id, None)
        self._save_index(index)

    def get_pinned_at(self, session_id: str) -> str | None:
        """Return the stored ``pinned`` timestamp, or ``None``."""
        index = self._load_index()
        stamp = index.get("pinned", {}).get(session_id)
        return stamp if isinstance(stamp, str) else None

    def record_project(self, session_id: str, project: str) -> None:
        """Append *project* to the session's recorded set, if new.

        Returns without writing when the project is already recorded, which is
        the common case: every turn of a bound session re-emits the same context.
        """
        index = self._load_index()
        bucket = index.setdefault("projects", {})
        current = list(bucket.get(session_id, []))
        if project in current:
            return
        current.append(project)
        bucket[session_id] = current
        self._save_index(index)

    def projects_for_session(self, session_id: str) -> list[str]:
        """Return every project identity recorded for a session."""
        index = self._load_index()
        return list(index.get("projects", {}).get(session_id, []))

    def tag_session(self, session_id: str, tag: str) -> None:
        """Associate a tag with a session ID for quick lookup."""
        index = self._load_index()
        index.setdefault("tags", {})[tag] = session_id
        self._save_index(index)

    def resolve_tag(self, tag: str) -> str | None:
        """Resolve a tag to a session ID, if present."""
        index = self._load_index()
        return index.get("tags", {}).get(tag)

    def list_tags(self) -> dict[str, str]:
        """Return a mapping of tags to session IDs."""
        index = self._load_index()
        return dict(index.get("tags", {}))

    def archive_session(self, session_id: str) -> None:
        """Mark a session as archived."""
        index = self._load_index()
        archived = index.setdefault("archived", {})
        archived[session_id] = _utc_now()
        self._save_index(index)

    def unarchive_session(self, session_id: str) -> None:
        """Remove archived status from a session."""
        index = self._load_index()
        archived = index.get("archived", {})
        if session_id in archived:
            archived.pop(session_id, None)
            index["archived"] = archived
            self._save_index(index)

    def is_archived(self, session_id: str) -> bool:
        """Return True if a session is archived."""
        index = self._load_index()
        archived = index.get("archived", {})
        return session_id in archived

    def terminate_session(self, session_id: str) -> bool:
        """Stamp ``terminated_at`` in the index once (never overwrites).

        Returns whether THIS call inserted the stamp — checked before the
        write so a repeat call reports ``False`` without touching the file.
        """
        index = self._load_index()
        terminated = index.setdefault("terminated", {})
        if session_id in terminated:
            self._mark_terminated_cached(session_id)
            return False
        terminated[session_id] = _utc_now()
        self._save_index(index)
        self._mark_terminated_cached(session_id)
        return True

    def get_terminated_at(self, session_id: str) -> str | None:
        """Return the stored ``terminated_at`` timestamp, or ``None``."""
        index = self._load_index()
        return index.get("terminated", {}).get(session_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_session_store(root_dir: str | None = None) -> SessionStoreBase:
    """Return the configured session store driver.

    Reads ``storage.driver`` from the app config.  Defaults to ``"json"``
    (filesystem).  Set to ``"mongodb"`` to use MongoDB.
    """
    driver = get_config_value("storage", "driver", default="json")
    if driver == "mongodb":
        from mewbo_core.session.session_store_mongo import MongoSessionStore

        try:
            return MongoSessionStore(root_dir=root_dir)
        except Exception as exc:
            raise RuntimeError(
                f"Storage driver is 'mongodb' but MongoDB is not available. "
                f"Check MEWBO_MONGODB_URI and ensure MongoDB is running. "
                f"Error: {exc}"
            ) from exc
    return SessionStore(root_dir=root_dir)


__all__ = [
    "SessionStoreBase",
    "SessionStore",
    "SessionPaths",
    "SessionRecord",
    "create_session_store",
]
