#!/usr/bin/env python3
"""The listing-shaped reduction of a session transcript.

A session summary folds a transcript, but it reads only a SPARSE slice of it:
the first event, every ``user``/``context``/``completion`` event, the two
resilience events, an outcome assertion, and the few ``tool_result`` events that
can prove a capability or carry line counts. Everything else — ``llm_call_start``
/ ``llm_call_end`` / ``agent_message_delta`` / ``permission`` and the 95 % of
``tool_result`` events that touched no file — is folded and discarded without
changing one field of the answer. Measured on the live store that surplus is
84 % of the events and **96 % of the bytes** (254.8 MB read, 10.6 MB needed).

:class:`SessionDigest` is that slice named as a value, so a LISTING can ask its
store for one per session instead of loading every transcript in a loop. Its
governing property, and the only thing any driver has to preserve:

    ``SessionRuntime.summarize_session(sid, events=digest.events)`` returns
    exactly what it returns for the full transcript.

That is what makes this a projection rather than a second summariser — there is
still ONE fold, in ``summarize_session``; only its input got smaller. A store
that pushes the selection down to its query engine must therefore keep that
query a SUPERSET of :meth:`SessionDigest.is_relevant`: fetching a surplus event
costs bytes, dropping a relevant one silently changes a row that still looks
plausible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from mewbo_core.contracts.diff_stat import DiffStat
from mewbo_core.contracts.types import EventRecord
from mewbo_core.session.session_provenance import CapabilityEvidence

# The two events that record a model-attributable failure. Declared here rather
# than beside their reader in ``loop/session_runtime.py`` because the selection
# and the fold must name the same set — a type the reader folds but the
# projection drops is a failure facet that vanishes from every listed row while
# the detail read still shows it.
RESILIENCE_EVENT_TYPES: frozenset[str] = frozenset({"llm_retry", "llm_fallback"})

# Types a summary reads by TYPE alone. ``tool_result`` is deliberately absent —
# it is the collection's fattest and most numerous type, and only the few that
# :meth:`SessionDigest.is_relevant` admits are worth reading.
_TYPED_SIGNALS: frozenset[str] = (
    frozenset({"user", "context", "completion", "outcome_assertion"})
    | RESILIENCE_EVENT_TYPES
    | CapabilityEvidence.evidence_event_types()
)


@dataclass(frozen=True)
class SessionDigest:
    """One session's listing row, carried as the events a summary folds.

    Plain and frozen rather than a Pydantic model: it crosses no trust boundary
    (it is assembled from records a driver already read) and a listing builds one
    per session, so validating every event again would be a per-row cost buying
    nothing. Same reasoning as ``RunHandle`` — see the root ``CLAUDE.md``.
    """

    session_id: str
    events: list[EventRecord] = field(default_factory=list)

    #: Event types a summary reads by type alone. A store MAY push this down.
    SIGNAL_TYPES: ClassVar[frozenset[str]] = _TYPED_SIGNALS

    @classmethod
    def is_relevant(cls, event: Mapping[str, object]) -> bool:
        """Return whether *event* can change a summary. ``O(1)``.

        THE definition of the projection, and the predicate every driver's
        selection has to be a superset of. Two arms:

        * a type in :data:`SIGNAL_TYPES` — read by type alone;
        * a ``tool_result`` that either proves a capability
          (:meth:`CapabilityEvidence.evidence_tool_ids`) or may carry line counts
          (:meth:`DiffStat.may_describe_edit`).

        Both arms delegate to the class that owns the rule, never to a copy of
        it. Nothing here parses a payload or diffs a string: this runs once per
        stored event on the file backend, so it has to stay a dict hit and a
        regex.
        """
        kind = event.get("type")
        if not isinstance(kind, str):
            return False
        if kind in cls.SIGNAL_TYPES:
            return True
        if kind != "tool_result":
            return False
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return False
        tool_id = payload.get("tool_id")
        if isinstance(tool_id, str) and tool_id in CapabilityEvidence.evidence_tool_ids():
            return True
        return DiffStat.may_describe_edit(payload)

    @classmethod
    def from_events(cls, session_id: str, events: Iterable[EventRecord]) -> SessionDigest:
        """Fold a session's events — in ts order — into its digest.

        ``O(one session's events)`` in time, ``O(relevant events)`` in memory:
        the surplus is tested and dropped, never accumulated, which is what lets
        a file-backed driver stream a 10,000-event transcript without holding it.

        The FIRST event is always kept whatever its type, because
        ``summarize_session`` reads ``events[0]["ts"]`` as the session's
        ``created_at``. Keeping the real record rather than synthesising a
        timestamp holder is deliberate — an invented event is one a future reader
        would fold as if the session had produced it.
        """
        kept: list[EventRecord] = []
        for index, event in enumerate(events):
            if index == 0 or cls.is_relevant(event):
                kept.append(event)
        return cls(session_id=session_id, events=kept)

    @classmethod
    def from_parts(
        cls,
        session_id: str,
        *,
        first_event: EventRecord | None,
        relevant: list[EventRecord],
    ) -> SessionDigest:
        """Assemble a digest a store selected in PIECES rather than folded.

        The seam for a driver whose query engine can return the two halves
        separately — the session's first event (one indexed lookup) and the
        relevant slice (one filtered read) — without ever materialising the
        transcript between them. *relevant* must already be in ts order.

        The first event is prepended only when the slice does not already start
        at it, compared on ``ts`` because that is the only field the prepend
        exists to fix: an equal timestamp yields an equal ``created_at``, so the
        prepend would be a duplicated record buying nothing.
        """
        if first_event is None:
            return cls(session_id=session_id, events=list(relevant))
        if relevant and relevant[0].get("ts") == first_event.get("ts"):
            return cls(session_id=session_id, events=list(relevant))
        return cls(session_id=session_id, events=[first_event, *relevant])


__all__ = ["RESILIENCE_EVENT_TYPES", "SessionDigest"]
