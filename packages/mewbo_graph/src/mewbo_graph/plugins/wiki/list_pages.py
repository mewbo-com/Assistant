"""``wiki_list_pages`` — page catalog for the wiki-qa agent.

This is the agent's "front door". Before deciding what to search for,
the agent can fetch the list of every page in the wiki and pick the
most plausibly-relevant one by title. Way cheaper than a BM25 search
when the question maps cleanly onto a single page.

It is also where the incremental refresh's per-page staleness HINT surfaces.
Each row carries the last refresh's verdict for that page plus the anchors that
actually moved (``DocPageNote.stale_anchor_keys`` — the evidence, not the canned
category), and stale rows sort first. **The hint RANKS; it does not command.**
The verdict is a heuristic score over anchor intersections, so a page it flags
may be perfectly accurate prose and a page it clears may still be wrong — the
model is expected to read a page before deciding, which is also what keeps a
human-edited page safe by default.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.list_pages")

# Per-page cap on the anchor evidence echoed into a tool result. The stored
# lists are already bounded by a page's own anchor count; this bounds the
# RESULT, which shares one character budget across every page in the catalog.
_MAX_ANCHORS = 10


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


class WikiListPagesArgs(BaseModel):
    """Arguments for ``wiki_list_pages``."""

    model_config = ConfigDict(extra="forbid")

    title_contains: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter on titles. Use to "
            "narrow a large catalog (e.g. 'auth' to find auth-related pages)."
        ),
    )
    stale_only: bool = Field(
        default=False,
        description=(
            "Only return pages the last incremental refresh flagged as stale "
            "(policy 'edit' or 'regenerate'). Each row carries the anchors that "
            "actually changed, so you can judge whether the page really needs "
            "updating. This is a RANKING hint, not an instruction: read the page "
            "before deciding, and leave it alone if the prose is still accurate."
        ),
    )


class WikiListPagesTool(WikiSessionTool):
    """SessionTool: list every wiki page for the QA session's slug."""

    tool_id = "wiki_list_pages"
    args_cls = WikiListPagesArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiListPagesArgs, name="wiki_list_pages"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_list_pages`` tool call."""
        ctx = self._qa_ctx()
        if ctx is None:
            return self._ungrounded_result()

        args = self._parse_args(WikiListPagesArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        try:
            pages = ctx.store.list_pages(ctx.slug)
        except Exception as exc:  # noqa: BLE001
            return _err_result("internal", f"list_pages failed: {exc}")

        # Best-effort: a wiki indexed before doc notes existed simply has none,
        # and the catalog must still list its pages. A store hiccup degrades the
        # HINT, never the front door.
        try:
            notes = {n.page_id: n for n in ctx.store.list_doc_notes(ctx.slug)}
        except Exception as exc:  # noqa: BLE001
            logging.warning("doc notes unavailable for {}: {}", ctx.slug, exc)
            notes = {}

        needle = (args.title_contains or "").strip().lower()
        results: list[dict[str, Any]] = []
        for p in pages:
            title = p.title or p.id
            if needle and needle not in title.lower():
                continue
            row: dict[str, Any] = {"pageId": p.id, "title": title}
            note = notes.get(p.id)
            if note is not None:
                row["policy"] = note.generation_policy
                row["staleness"] = note.staleness_score
                row["reason"] = note.staleness_reason
                if note.stale_anchor_keys:
                    # Bounded on the way out as well as in the store: a page's
                    # anchor count is small but not guaranteed small, and this
                    # rides a tool result that the loop caps by characters.
                    row["changedAnchors"] = note.stale_anchor_keys[:_MAX_ANCHORS]
                    row["changedAnchorCount"] = len(note.stale_anchor_keys)
                if note.deleted_anchor_keys:
                    row["deletedAnchors"] = note.deleted_anchor_keys[:_MAX_ANCHORS]
            if args.stale_only and row.get("policy", "keep") == "keep":
                continue
            results.append(row)

        # Stale first, most stale first within that — the RANKING half of the
        # hint. Ties and unflagged pages fall back to alphabetical, so output
        # stays stable and scannable for the ordinary catalog use.
        results.sort(key=lambda r: (-float(r.get("staleness") or 0.0), r["title"].lower()))
        return MockSpeaker(content=str({"pages": results, "count": len(results)}))


__all__ = ["WikiListPagesArgs", "WikiListPagesTool"]
