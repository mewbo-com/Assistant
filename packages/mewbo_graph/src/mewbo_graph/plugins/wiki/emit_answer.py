"""``wiki_emit_answer`` SessionTool — deliver the COMPLETE answer in one atomic call.

Contract-shape rationale: models naturally compose a whole answer in one pass.
The previous per-block choreography (``wiki_emit_block`` × N with index
bookkeeping) fought that grain — weaker models narrated the call sequence as
plain text and delivered nothing. One schema-validated call carrying the full
``blocks`` array is the ``EmitStructuredResponseTool`` pattern: a malformed
payload round-trips through the tool-result feedback loop (the model retries),
and a valid one IS the answer's accept state. The per-block ``block_open``/
``block_close`` QA events are still emitted server-side, in order, so the SSE
stream, console renderer, and ``QaFinalizer`` reconciliation are untouched.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import resolve_runtime

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.emit_answer")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiEmitAnswerArgs(BaseModel):
    """Arguments for ``wiki_emit_answer``."""

    model_config = ConfigDict(extra="forbid")

    blocks: list[dict[str, Any]] = Field(
        min_length=2,
        description=(
            "The COMPLETE answer as an ordered array of blocks "
            "(kind: p|h2|h3|hr|ul|table), ending with exactly one "
            "{kind: 'sources', items: [...]} block LAST. One call delivers "
            "the whole answer — there is no other output channel."
        ),
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


def _qa_diagram_text(blocks: list[dict[str, Any]]) -> str:
    """Concatenate every block's string-typed text for Mermaid-fence scanning.

    A QA answer has no page-body markdown blob to hand the validator — the
    fence lives inside a block's own text field. Only plain-string content is
    scanned; a fence would never be authored inside a nested inline-citation
    node, so skipping non-string ``text``/list/dict shapes is deliberate, not
    a gap.
    """
    parts: list[str] = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
        items = block.get("items")
        if isinstance(items, list):
            parts.extend(item for item in items if isinstance(item, str))
        rows = block.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, list):
                    parts.extend(cell for cell in row if isinstance(cell, str))
    return "\n".join(parts)


class WikiEmitAnswerTool(WikiSessionTool):
    """SessionTool: validate the full answer and persist its block events atomically."""

    tool_id = "wiki_emit_answer"
    args_cls = WikiEmitAnswerArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiEmitAnswerArgs, name="wiki_emit_answer"
    )
    # The completion seam's required-terminal gate (SessionTool declaration
    # law, see tooling/CLAUDE.md): a QA run may not reach a clean natural
    # completion without a call to THIS tool — a composed answer delivered as
    # plain text instead is discarded, and the backing QaAnswer would
    # otherwise strand at status "running" forever.
    required_terminal = True

    def should_terminate_run(self) -> bool:
        """A valid call IS the answer's accept state — the loop stops cleanly here."""
        return getattr(self, "_terminate", False)

    def terminal_satisfied(self) -> bool:
        """The required-terminal gate's attestation — off the SAME success flag.

        ``_terminate`` is only ever set True at the tail of a successful
        ``handle()`` (step 8, after the answer is fully validated and
        persisted), so reusing it here rather than a second flag keeps the two
        questions ("should the run stop" / "was the obligation met") from ever
        answering differently for the same call.
        """
        return getattr(self, "_terminate", False)

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_emit_answer`` tool call."""
        # 1. Resolve runtime and QA ctx.
        ctx = self._qa_ctx()
        if ctx is None:
            return _err_result("internal", "wiki QA ctx not found for this session")
        # A grounded structured-response session resolves a slug-only ctx
        # (``answer_id is None``) — it has no QA event log to write blocks into.
        if ctx.answer_id is None:
            return _err_result(
                "internal", "wiki_emit_answer requires a registered QA answer"
            )

        # 2. Parse and validate outer args.
        args = self._parse_args(WikiEmitAnswerArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Validate every block against the discriminated union; positional
        #    errors ride the tool-result feedback loop so the model retries.
        from mewbo_graph.wiki.types import BlockUnion  # noqa: PLC0415

        validated: list[dict[str, Any]] = []
        for i, raw in enumerate(args.blocks):
            try:
                validated.append(BlockUnion.model_validate(raw).model_dump(by_alias=True))
            except Exception as exc:
                return _err_result("validation", f"blocks[{i}] invalid: {exc}")

        # 4. Contract: exactly ONE sources block, and it is LAST.
        source_positions = [i for i, b in enumerate(validated) if b.get("kind") == "sources"]
        if source_positions != [len(validated) - 1]:
            return _err_result(
                "validation",
                "the answer must end with exactly one {kind: 'sources'} block as "
                "its LAST element — re-send the full blocks array with the "
                "sources block appended last",
            )

        # 4.5. Mermaid gate — refuse the emit, never terminate, so the model repairs
        # in-band via the same tool-result feedback loop the block-validation errors
        # above already use. Nothing has been persisted yet (events are appended in
        # step 7 below), so a rejection here discards no prior work — unlike the
        # wiki-finalize gate (finalize.py), which refuses AFTER pages are already
        # saved; here there is nothing to preserve because there is nothing yet.
        from mewbo_graph.plugins.wiki.mermaid import MermaidValidator  # noqa: PLC0415

        repairs = MermaidValidator().inspect(f"qa:{ctx.answer_id}", _qa_diagram_text(validated))
        if repairs:
            instructions = "; ".join(r.instruction for r in repairs)
            return _err_result(
                "validation",
                f"{len(repairs)} invalid Mermaid diagram(s) in this answer would "
                f"render as an error. Repair and re-send the full blocks array: "
                f"{instructions}",
            )

        # 5. Atomicity guard — the answer is delivered once PER TURN. The turn
        #    boundary is asked of ``QaFinalizer.current_turn_events``, the ONE
        #    place that rule lives, rather than re-derived here: this guard and
        #    the reconciliation that follows must agree on what a turn is, and
        #    scanning the whole cumulative log made them disagree in both
        #    directions — a follow-up turn was refused because the PRIOR turn
        #    had emitted, while a re-drive of a failed turn was admitted and then
        #    discarded by a close that still saw the failed turn's verdict.
        from mewbo_graph.wiki.qa import QaFinalizer  # noqa: PLC0415

        turn_events = QaFinalizer.current_turn_events(ctx.store.load_qa_events(ctx.answer_id))
        if any(ev.get("type") == "block_open" for ev in turn_events):
            return _err_result("validation", "the answer was already emitted")

        # 6. Re-scheme bare wiki-page citations on the sources block BEFORE it
        #    lands on the log — one seam fixes both the live stream and the
        #    reconciled snapshot.
        validated[-1] = QaFinalizer.tag_page_citations(validated[-1], ctx.store, ctx.slug)

        # 7. Fan the array into the existing per-block event contract, in order —
        #    SSE stream, console renderer, and snapshot reconciliation unchanged.
        for index, block_dict in enumerate(validated):
            ctx.store.append_qa_event(ctx.answer_id, {
                "type": "block_open",
                "index": index,
                "block": block_dict,
            })
            ctx.store.append_qa_event(ctx.answer_id, {
                "type": "block_close",
                "index": index,
            })

        # 8. Accept state: reconcile + close the answer and terminate the run.
        #    The on_session_end net still covers a run that never called this.
        QaFinalizer.close(ctx.store, ctx.answer_id)
        self._terminate = True

        return MockSpeaker(content=str({"ok": True, "blocks": len(validated)}))


__all__ = ["WikiEmitAnswerArgs", "WikiEmitAnswerTool"]
