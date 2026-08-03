#!/usr/bin/env python3
"""Lossless pre-compaction utilities.

This module intentionally contains only one thing: a ``pre_compact`` hook
that strips ANSI escapes and truncates huge tool outputs before the LLM
summarizer sees them. Zero tokens, zero risk.

Prior versions also shipped ``should_compact`` (an event-count heuristic)
and ``summarize_events`` (a fallback "summary" that concatenated raw
event text). Both were deleted: compaction decisions are now driven
purely by the API-reported ``usage_metadata.input_tokens``, and failed
structured compaction must not be masked with raw-text noise.
"""

from __future__ import annotations

import re

from mewbo_core.common import get_logger
from mewbo_core.contracts.types import EventRecord

logging = get_logger(name="core.compaction")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# The THIRD independent cap a tool result passes, and the only permanent one:
# after compaction the model's memory of any result is whatever survives here,
# regardless of what it originally read. It is deliberately NOT the tool's own
# cap (``read_file`` declares 200_000) — this text becomes the SUMMARIZER's
# input, a token cost paid on every compaction over every retained result, and
# matching the tool cap would multiply that by a hundred to preserve detail the
# summarizer exists to discard.
#
# The arithmetic behind 2x rather than more, so the next reader knows this small
# number was CHOSEN and not inherited: a compaction window holding ~40 tool
# results costs ~500 tokens each at 2000 chars, so 2x adds roughly 2k tokens per
# compaction — affordable. The tool cap (200_000) would make the same window
# unbounded in practice, on a call whose entire purpose is to shrink the context.
# 4000 is enough to carry a stack trace or a short file past the boundary.
_MAX_RESULT_CHARS = 4000


def micro_compact_events(events: list[EventRecord]) -> list[EventRecord]:
    """Lossless pre-compaction: strip ANSI escapes and truncate large tool outputs.

    Intended for use as a ``pre_compact`` hook — no LLM call, zero cost.

    The two jobs are INDEPENDENT and applied as such. They used to be one
    branch, so an escape-laden result under the cap kept every escape — and
    raising the cap silently widened that band. Stripping is unconditional;
    only the truncation consults the cap.
    """
    compacted: list[EventRecord] = []
    for event in events:
        if event.get("type") != "tool_result":
            compacted.append(event)
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            compacted.append(event)
            continue
        result = payload.get("result")
        if not isinstance(result, str):
            compacted.append(event)
            continue
        stripped = _ANSI_RE.sub("", result)
        if len(result) > _MAX_RESULT_CHARS:
            # State the omission's SIZE. A mute marker leaves a bounded memory
            # indistinguishable from a complete one, which is how a model ends
            # up reasoning from the first fraction of a result as though it
            # were the whole — and arguing with a tool that told it no such
            # limit existed. Sized against the ORIGINAL, which is what the
            # model saw before compaction.
            omitted = len(result) - _MAX_RESULT_CHARS
            stripped = (
                stripped[:_MAX_RESULT_CHARS]
                + f"\n[... {omitted} of {len(result)} characters omitted by compaction]"
                + "\n[truncated]"
            )
        elif stripped == result:
            compacted.append(event)
            continue
        payload = dict(payload)
        payload["result"] = stripped
        compacted.append({**event, "payload": payload})
    return compacted
