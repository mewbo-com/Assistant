#!/usr/bin/env python3
"""The ``after`` cursor a transcript poll carries, as one value.

A client polling ``/events`` sends back the ``ts`` of the last event it saw and
expects everything newer. Spread across a module function in the runtime and an
implicit assumption in every caller, that one rule goes wrong in all three
directions a filter can:

* **It failed OPEN.** An unparseable cursor returned the WHOLE transcript with a
  ``200``, so a client bug became a full-collection transfer that neither end
  could see — 14.8 MB on the largest live session. The live footgun is this
  API's own timestamps: they contain ``+``, which a client that echoes one back
  without percent-encoding sends as a space.
* **It raised.** Comparing a stored offset-naive timestamp against an
  offset-aware cursor is a ``TypeError`` in Python, not a ``False`` — a poll
  path that meets one 500s.
* **It narrowed the RESPONSE and not the WORK.** Filtering in Python after
  loading the whole transcript is a cursor in name only: the reply was 397 B
  and the read was still the record's entire history.

:class:`EventCursor` is that rule as an atomic value — parse once at the edge,
then hand it around. It owns the parse (so an HTTP boundary can refuse a bad one
without re-implementing ``fromisoformat`` and drifting), the exact comparison,
and :attr:`store_floor`, the coarse bound a store may push into its query. I/O
free: events arrive as method arguments, never read here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class EventCursor:
    """A parsed ``after`` cursor: "every event strictly newer than this instant"."""

    at: datetime

    @classmethod
    def parse(cls, raw: str | None) -> EventCursor | None:
        """Parse a cursor, or return ``None`` when it is NOT a timestamp.

        ``None`` means UNPARSEABLE and nothing else — an ABSENT cursor is the
        caller's own case and must be handled before calling this, because the
        two demand opposite behaviour: no cursor means "send everything", while
        a broken cursor must be REFUSED. Collapsing them is precisely how the
        fail-open defect existed, so the distinction is pushed onto the caller
        rather than hidden in a return value.

        An offset-naive value is read as UTC — the spelling the store itself
        writes — rather than rejected. It is a real timestamp that a client is
        entitled to send, and it is the same normalisation :meth:`matches`
        applies to a stored one, so the two sides cannot disagree about what an
        unqualified instant means.
        """
        if not raw:
            return None
        moment = cls._read(raw)
        return None if moment is None else cls(at=moment)

    def matches(self, event: Mapping[str, object]) -> bool:
        """Return whether *event* is strictly newer than this cursor. ``O(1)``.

        THE decision, and deliberately total: an event whose ``ts`` is missing,
        malformed or not a string is treated as NOT newer rather than raising.
        A single bad record must not break a poll for the whole session, and
        the raising comparison (an offset-naive stored value against an
        offset-aware cursor) is a 500 on the hot path rather than a dropped row.
        """
        raw = event.get("ts")
        moment = self._read(raw) if isinstance(raw, str) else None
        return moment is not None and moment > self.at

    @property
    def store_floor(self) -> str:
        """A lexicographic lower bound a store may push into its query.

        The cursor's instant in the store's own spelling
        (``datetime.now(timezone.utc).isoformat()``), truncated to the second.
        Truncating is what makes it a SUPERSET rather than an equivalent: within
        that spelling a sub-second event sorts above the whole second (``.`` is
        above ``+``), so every event this bound admits is a candidate and no
        event newer than the cursor is ever below it. The surplus is at most the
        events sharing one second with the cursor, and :meth:`matches` still
        decides.

        **Its soundness rests on every stored ``ts`` being written in that one
        spelling**, which is why the append seam canonicalises a caller-supplied
        timestamp instead of storing it verbatim: a value carrying a different
        UTC offset would order differently as TEXT than as an instant, and a
        pushed-down bound would then drop a real event with nothing raised
        anywhere.
        """
        return self.at.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def canonical(raw: object) -> str | None:
        """Return *raw* in the store's timestamp spelling, or ``None``.

        The append seam's normaliser, kept here so the writer and the reader
        share one definition of what a stored timestamp looks like. What it
        normalises is the UTC OFFSET — a value carrying a different one orders
        differently as text than as an instant, and every ``ts`` comparison in
        the store is textual. The instant is never moved.

        Microseconds are always written out, which is what makes this a no-op
        for the timestamps a client actually mirrors: they come from the same
        ``utc_now_iso()`` this store uses, so a record already in the store's
        spelling is stored byte-identical and mirror fidelity is preserved.
        (Bare ``isoformat()`` would silently drop a zero microsecond field and
        rewrite exactly those records for no gain.)

        ``None`` means "not a timestamp I can normalise" — the caller stores the
        original rather than inventing one, since a value nobody can parse is
        still evidence of what a client claimed and a fabricated one is not.
        """
        moment = EventCursor._read(raw)
        if moment is None:
            return None
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _read(raw: object) -> datetime | None:
        """Parse an ISO-8601 string to an AWARE datetime, or ``None``."""
        if not isinstance(raw, str) or not raw:
            return None
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


__all__ = ["EventCursor"]
