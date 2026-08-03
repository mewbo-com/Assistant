"""Shared base class for the wiki SessionTools.

Every wiki built-in tool is a ``SessionTool`` constructed per agent with a
``session_id``. They all share the same lifecycle boilerplate — the ctor, the
``should_terminate_run`` flag, the runtime/ctx resolution, validating args and
serialising the result. That boilerplate lived copy-pasted across ~14 modules
(the exact smell ``source_tools._SourceToolShim`` and
``graph_neighbors.WikiGraphNeighbors`` already factored out for their suites).

``WikiSessionTool`` is the one home for it. A concrete tool subclasses it, sets
``tool_id``/``args_cls``/``schema``, and implements :meth:`run` over a resolved
ctx + validated args; everything else is inherited.

Test seam: each tool module keeps a module-level ``_resolve_runtime`` function
that delegates to :func:`mewbo_graph.plugins.wiki._ctx.resolve_runtime`. The
base resolves it through the subclass's own module at call time so existing
tests can still ``patch.object(<tool_module>, "_resolve_runtime", ...)``.
"""
from __future__ import annotations

import json
import sys
import types as _pytypes
import typing
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker
from mewbo_core.tooling.session_tools import DEFAULT_SESSION_TOOL_MODES
from pydantic import BaseModel, ValidationError

from mewbo_graph.plugins.wiki._ctx import (
    SessionProject,
    WikiJobCtx,
    WikiQaCtx,
    resolve_job_ctx,
    resolve_qa_ctx,
    resolve_runtime,
)
from mewbo_graph.wiki.qa_access import QaAccessRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.contracts.types import Event


def _err_result(code: str, message: str) -> MockSpeaker:
    """Return a MockSpeaker carrying a structured error payload."""
    return MockSpeaker(content=str({"error": {"code": code, "message": message}}))


class WikiSessionTool:
    """Base for all wiki SessionTools — owns the shared lifecycle.

    Subclasses set the class attributes ``tool_id``/``args_cls``/``schema`` and
    implement :meth:`run`. The base supplies the ctor, ``should_terminate_run``,
    runtime resolution (patchable per-module test seam), arg validation and the
    structured error/result serialisation.
    """

    tool_id: str = ""
    args_cls: type[BaseModel] = BaseModel
    schema: dict[str, object] = {}
    modes = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id.

        ``event_logger`` is part of the SessionTool construction signature but
        is unused — all wiki event emission goes through the store
        (``append_job_event`` / ``emit_log`` / ``append_qa_event``).
        """
        self._session_id = session_id

    def should_terminate_run(self) -> bool:
        """Wiki tools never request loop termination (no tool sets a flag)."""
        return False

    def terminal_reason(self) -> str:
        """done_reason when a wiki terminal tool stops the run.

        Both wiki terminal tools (``wiki_finalize`` and the atomic ``wiki_emit_answer``
        accept-state) signal a *successful* terminal state, so the base
        default is ``"completed"`` — never the exit_plan_mode approval gate. A
        terminal tool can no longer override ``should_terminate_run`` while
        lacking a ``terminal_reason`` (a contract violation).
        """
        return "completed"

    # ── Resolution helpers (shared) ─────────────────────────────────────

    def _runtime(self) -> Any | None:
        """Resolve the wiki runtime via the subclass module's ``_resolve_runtime``.

        Honours per-module test patching: tests do
        ``patch.object(<tool_module>, "_resolve_runtime", ...)``. Falls back to
        the canonical ``_ctx.resolve_runtime`` when a module declares no alias.
        """
        module = sys.modules.get(type(self).__module__)
        resolver = getattr(module, "_resolve_runtime", resolve_runtime)
        return resolver()

    def _job_ctx(self) -> WikiJobCtx | None:
        """Resolve the indexing-job ctx for this session, or ``None``."""
        runtime = self._runtime()
        return resolve_job_ctx(self._session_id, runtime) if runtime is not None else None

    def _qa_ctx(self) -> WikiQaCtx | None:
        """Resolve the QA ctx for this session, or ``None``."""
        runtime = self._runtime()
        return resolve_qa_ctx(self._session_id, runtime) if runtime is not None else None

    def _ungrounded_result(self) -> MockSpeaker:
        """Return the envelope for "this session has no wiki to read".

        The read/navigate tools bind to ORDINARY sessions, so failing to resolve
        a slug is an EXPECTED outcome, not a fault — hence ``not_found`` rather
        than ``internal``, and hence a message that names the project it looked
        for. An agent must be able to learn from ONE call that there is nothing
        here and move on, instead of re-trying against what reads as a transient
        internal error. Resolved lazily, on the miss path only.
        """
        label = SessionProject.for_session(self._session_id, self._runtime()).label
        return _err_result("not_found", f"no wiki indexed for {label}")

    @staticmethod
    def _record_qa_access(ctx: Any, records: list[QaAccessRecord]) -> None:
        """Record the bounded, score-ranked retrieval trail for a QA probe.

        The deterministic provenance trail (vs the LLM's hand-picked citations):
        each retrieval tool reports the refs it grounded against as typed
        :class:`QaAccessRecord`s — scored search hits carry their real ``score``/
        ``rank``; navigation seeds and file/page reads are unscored grounding
        touches. The finalizer (:meth:`QaFinalizer._accessed_from_events`) dedupes,
        score-orders, and caps them into ``QaAnswer.accessed_sources`` — so the
        trail stays a tight, high-signal list instead of the full unranked
        graph-navigation set. A no-op outside a QA ctx (an indexing job ctx has no
        ``answer_id``) and best-effort — telemetry never breaks a retrieval call.
        ``ref`` always uses the citation id grammar: ``graph:<node_id>`` /
        ``<path>#L<a>-<b>`` (or bare ``<path>``) / ``wiki:<page_id>``.
        """
        answer_id = getattr(ctx, "answer_id", None)
        store = getattr(ctx, "store", None)
        clean = [r for r in records if r and r.ref]
        if not answer_id or store is None or not clean:
            return
        try:
            store.append_qa_event(
                answer_id,
                {"type": "access", "records": [r.model_dump() for r in clean]},
            )
        except Exception:  # pragma: no cover — provenance is best-effort
            pass

    # ── Arg parsing + result serialisation (shared) ─────────────────────

    @staticmethod
    def _container_origins(annotation: Any) -> tuple[type, ...]:
        """Return the ``list``/``dict`` origins *annotation* accepts, if any.

        Unwraps unions so an optional field (``list[str] | None``) reports the
        same container its non-optional twin does.
        """
        origin = typing.get_origin(annotation)
        if origin in (typing.Union, _pytypes.UnionType):
            found: list[type] = []
            for member in typing.get_args(annotation):
                found.extend(WikiSessionTool._container_origins(member))
            return tuple(dict.fromkeys(found))
        if origin in (list, dict):
            return (origin,)
        if annotation in (list, dict):
            return (annotation,)
        return ()

    @staticmethod
    def _decode_json_valued_fields(
        args_cls: type[BaseModel], raw: dict[str, Any]
    ) -> dict[str, Any]:
        """Decode a JSON STRING standing in for a declared list/dict argument.

        Models routinely serialise a structured argument as a JSON string
        instead of the array the schema declares, and Pydantic rejects it
        (``type=list_type``). The tool call then fails and the model spends a
        whole round trip re-sending the same content in a different shape — on
        this deployment ``wiki_emit_answer`` failed that way on 5 of its 8
        recorded failures, and one Q&A answer took three attempts to land, so
        the user waited out two extra model turns for an answer that had
        already been composed correctly the first time.

        It is NOT one provider's quirk, which is why the repair belongs here
        rather than behind a model check: the same failure is on record from
        five different model families across four different wiki/scg tools.

        Deliberately narrow, so a real mistake still surfaces as itself:
        a value is rewritten only when the field DECLARES a list or dict, the
        supplied value is a string, and that string parses to the declared
        container. A model that also wrapped the array in a single-key object
        (``'{"pages": [...]}'`` for a ``pages`` field) is unwrapped for the
        same reason — it is the same content under one more layer, and the
        alternative is refusing an answer we can read perfectly well.
        """
        decoded: dict[str, Any] | None = None
        for name, field in args_cls.model_fields.items():
            key = name if name in raw else (field.alias if field.alias in raw else None)
            if key is None:
                continue
            value = raw[key]
            if not isinstance(value, str):
                continue
            wanted = WikiSessionTool._container_origins(field.annotation)
            if not wanted:
                continue
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                continue
            # A single-key wrapper naming this very field is the same payload
            # one layer down; anything else keyed differently is not ours to
            # reinterpret.
            if isinstance(parsed, dict) and not isinstance(parsed, wanted):
                inner = parsed.get(name, parsed.get(field.alias or name))
                if isinstance(inner, wanted):
                    parsed = inner
            if not isinstance(parsed, wanted):
                continue
            if decoded is None:
                decoded = dict(raw)
            decoded[key] = parsed
        return decoded if decoded is not None else raw

    @staticmethod
    def _parse_args(args_cls: type[BaseModel], action_step: ActionStep) -> Any:
        """Validate ``action_step.tool_input`` against *args_cls*.

        Returns the validated model on success, or a :class:`MockSpeaker`
        carrying a structured ``validation`` error the caller can return as-is.
        """
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        raw = WikiSessionTool._decode_json_valued_fields(args_cls, raw)
        try:
            return args_cls.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))


__all__ = ["WikiSessionTool", "_err_result"]
