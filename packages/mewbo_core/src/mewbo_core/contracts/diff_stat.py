"""Line-level diff arithmetic — the ONE home for ``+N -M``.

Three surfaces show added/deleted lines, and counting independently at each one
lets them disagree: reconstructing old/new sides from a unified diff and
RE-diffing them under-reports (``SequenceMatcher`` rematches across hunk
boundaries), while tallying ``+``/``-`` lines in the renderer counts hunk headers
and context markers. :class:`DiffStat` is that arithmetic as one atomic model, so
a count is computed ONCE at the producer and then travels.

It lives in ``mewbo_core`` because both ``mewbo_tools`` (which EMITS the counts
alongside a diff) and the CLI (which RENDERS them) need it, and the dependency
DAG only ever flows down into core.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

# The ``kind`` discriminator of the diff document that
# ``mewbo_tools.integration.edit_common.format_diff_result`` writes and every
# diff-rendering surface reads. Declared here because this module owns both ends
# of that contract: it produces the ``additions``/``deletions`` the document
# carries and it parses the document back out of a transcript.
DIFF_RESULT_KIND = "diff"

# Tool ids whose RAW (envelope-less) results still describe a file edit, matched
# on the id exactly as the console does (``utils/logs.ts``). External and MCP edit
# tools (``Edit``, ``Write``, patch variants) never route through
# ``format_diff_result``, so without this leg the great majority of edit-shaped
# results would contribute nothing.
EDIT_TOOL_ID_RE = re.compile(r"edit|write|patch", re.IGNORECASE)

# The parse GATE for the string-shaped envelope — never the decision, which stays
# the parsed ``kind``. Tighter than a bare ``DIFF_RESULT_KIND in result``
# substring test and sound for anything a JSON encoder produced: a document whose
# top level really is ``{"kind": "diff", ...}`` cannot be serialised without the
# key, a colon and the value appearing in that order. The tightening is what makes
# the gate PUSHABLE — a listing that has to fetch every result merely MENTIONING a
# diff reads 45.7 MB where the real envelopes are 3.1 MB (measured live: 2,832
# substring hits, 432 envelopes), and the surplus is tools that only READ
# diff-shaped bytes. See :meth:`DiffStat.may_describe_edit`.
DIFF_ENVELOPE_RE = re.compile(r'"kind"\s*:\s*"' + DIFF_RESULT_KIND + '"')


class DiffStat(BaseModel):
    """Added/deleted line counts for one edit, or a sum over many.

    Frozen and additive: a caller accumulates with ``+`` rather than mutating a
    running pair of ints, which is what lets the same value be a per-edit fact
    and a per-session rollup without two shapes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    additions: int = Field(
        default=0, ge=0, description="Lines added by the edit (or edits)."
    )
    deletions: int = Field(
        default=0, ge=0, description="Lines removed by the edit (or edits)."
    )

    # -- construction ------------------------------------------------------

    @classmethod
    def from_unified_diff(cls, text: str) -> DiffStat:
        """Tally ``+``/``-`` lines in unified-diff *text*.

        The ``+++``/``---`` file headers are excluded, which is what makes this
        the honest line tally and keeps it in step with what diff2html reports on
        the console side. Hunk headers (``@@``) and context lines contribute
        nothing. A truncation marker appended to a clipped diff starts with
        neither sign, so a truncated diff degrades to an undercount rather than a
        wrong count.
        """
        additions = deletions = 0
        for line in text.splitlines():
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        return cls(additions=additions, deletions=deletions)

    @classmethod
    def from_texts(cls, old: str, new: str) -> DiffStat:
        """Count the lines an edit from *old* to *new* changed.

        Opcode counting over ``difflib.SequenceMatcher``: a ``replace`` counts on
        both sides, so a rewritten line is one addition AND one deletion. Use
        this only when both sides are genuinely in hand — deriving them by
        reconstructing a unified diff loses hunk boundaries, and the matcher will
        then pair lines across hunks that the diff kept apart.
        """
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        additions = deletions = 0
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                deletions += i2 - i1
            if tag in ("replace", "insert"):
                additions += j2 - j1
        return cls(additions=additions, deletions=deletions)

    @classmethod
    def from_tool_result(cls, payload: Mapping[str, object]) -> DiffStat:
        """Read whatever line counts a ``tool_result`` event payload supports.

        Two legs, tried in order, and never both for one result:

        1. **The diff envelope** — the ``{"kind": "diff", ...}`` document that
           ``format_diff_result`` writes. Its ``additions``/``deletions`` keys are
           preferred; a document written before those keys existed falls back to
           re-tallying its ``text``.
        2. **Synthesized from the edit arguments** — a raw result whose
           ``tool_input`` carries ``old_string``/``new_string`` (or ``content``).
           Subordinate to the envelope on purpose: it is a reconstruction, not a
           record. The console synthesizes a diff card from the same fields under
           the same predicate (``utils/logs.ts``), so the two are a pair — a
           change to either predicate belongs in both.

        Shell-driven edits (``git apply``, heredocs) are deliberately NOT
        captured. They leave nothing structured behind, and a guess made from a
        shell command's text would be worse than an honest omission.

        Never raises: a malformed, truncated, or simply unrelated result is worth
        zero lines, not a broken session listing.
        """
        # A failed call produced no lines. Checked as ``is False`` so a payload
        # with no ``success`` key still counts.
        if payload.get("success") is False:
            return cls()
        document = cls._diff_document(payload.get("result"))
        if document is not None:
            return cls._from_diff_document(document)
        return cls._from_edit_arguments(payload)

    @classmethod
    def may_describe_edit(cls, payload: Mapping[str, object]) -> bool:
        """Cheap gate for :meth:`from_tool_result` — a SUPERSET, never narrower.

        The union of the two legs' own entry conditions and nothing else, so a
        payload this rejects is guaranteed to fold to an empty stat. That
        guarantee is the whole point: it lets a store push the candidate filter
        down and select the handful of ``tool_result`` events a LISTING has to
        read, instead of every one ever written. ``O(1)`` — one regex over a
        tool id and one over the result string, no JSON parse and no
        ``SequenceMatcher``.

        It must stay derived from the same two constants the legs use
        (:data:`DIFF_ENVELOPE_RE`, :data:`EDIT_TOOL_ID_RE`) rather than restating
        them: a store-side filter that drifts NARROWER than this drops a row's
        ``+N -M`` silently, with no error anywhere and a listing that still
        looks right.
        """
        result = payload.get("result")
        if isinstance(result, Mapping):
            if result.get("kind") == DIFF_RESULT_KIND:
                return True
        elif isinstance(result, str) and DIFF_ENVELOPE_RE.search(result):
            return True
        tool_id = payload.get("tool_id")
        return isinstance(tool_id, str) and EDIT_TOOL_ID_RE.search(tool_id) is not None

    # -- arithmetic --------------------------------------------------------

    def __add__(self, other: object) -> DiffStat:
        """Sum two stats, so a caller can fold a whole transcript into one."""
        if not isinstance(other, DiffStat):
            return NotImplemented
        return DiffStat(
            additions=self.additions + other.additions,
            deletions=self.deletions + other.deletions,
        )

    @property
    def is_empty(self) -> bool:
        """True when nothing changed — the caller's cue to omit the field."""
        return self.additions == 0 and self.deletions == 0

    def render(self) -> str:
        """The compact ``+N -M`` label every surface shows."""
        return f"+{self.additions} -{self.deletions}"

    # -- payload readers ---------------------------------------------------

    @staticmethod
    def _diff_document(result: object) -> Mapping[str, object] | None:
        """Return the diff document *result* carries, or ``None``.

        A tool result reaches the transcript as a JSON STRING whenever the tool
        returned a dict, so both shapes are accepted. :data:`DIFF_ENVELOPE_RE`
        only GATES the parse — the decision is the PARSED ``kind``, because a tool
        that merely READ a diff-shaped document (a page fetch, a code search over
        this repository) quotes the same bytes without having written a line.
        """
        if isinstance(result, Mapping):
            return result if result.get("kind") == DIFF_RESULT_KIND else None
        if not isinstance(result, str) or not DIFF_ENVELOPE_RE.search(result):
            return None
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict) and parsed.get("kind") == DIFF_RESULT_KIND:
            return parsed
        return None

    @classmethod
    def _from_diff_document(cls, document: Mapping[str, object]) -> DiffStat:
        """Prefer the document's recorded counts, else re-tally its text."""
        additions = cls._count(document.get("additions"))
        deletions = cls._count(document.get("deletions"))
        if additions is not None and deletions is not None:
            return cls(additions=additions, deletions=deletions)
        text = document.get("text")
        return cls.from_unified_diff(text if isinstance(text, str) else "")

    @classmethod
    def _from_edit_arguments(cls, payload: Mapping[str, object]) -> DiffStat:
        """Synthesize a stat from an edit tool's own arguments."""
        tool_id = payload.get("tool_id")
        if not isinstance(tool_id, str) or not EDIT_TOOL_ID_RE.search(tool_id):
            return cls()
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            return cls()
        if not cls._text(tool_input.get("file_path")):
            return cls()
        old = cls._text(tool_input.get("old_string"))
        # A write of a new file names no ``old_string``, so it counts as all
        # additions — which is exactly right.
        new = cls._text(tool_input.get("new_string")) or cls._text(
            tool_input.get("content")
        )
        if not old and not new:
            return cls()
        return cls.from_texts(old, new)

    @staticmethod
    def _count(value: object) -> int | None:
        """Coerce a recorded count, rejecting anything not a non-negative int."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _text(value: object) -> str:
        """Coerce a tool argument to a string, or ``""`` when it is not one."""
        return value if isinstance(value, str) else ""


__all__ = ["DIFF_ENVELOPE_RE", "DIFF_RESULT_KIND", "EDIT_TOOL_ID_RE", "DiffStat"]
