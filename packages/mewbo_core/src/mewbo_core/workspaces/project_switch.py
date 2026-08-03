#!/usr/bin/env python3
"""The agent-facing half of project selection: list the catalog, then move into one.

Two SessionTools over the ONE resolver in :mod:`mewbo_core.workspaces.project_catalog`:
``list_projects`` reads the catalog, ``switch_project`` re-points the running
loop at one of its entries. Neither owns any resolution logic of its own — the
catalog is the single grammar, and a second copy here is exactly the drift that
made "project name to path" five divergent implementations before it existed.

**Both tools are ROOT-only, and that is a scoping decision rather than an
oversight.** A spawned child is handed a workspace by its parent; letting it
re-scope the session would mean a leaf agent could move the ground under every
sibling still working in the directory it left. The injection seam in
:class:`~mewbo_core.loop.tool_use_loop.ToolUseLoop` enforces it (``depth == 0``),
which is also why these tools take a ``rebind`` CALLABLE rather than a loop
reference: the tool states the intent, the loop owns the mutation, and nothing
here can reach a loop it was not handed.

The switch is a real mid-run mutation, not a note for later — by the time
``handle`` returns, the working directory, the filesystem containment root, the
project instructions, the tool registry and every future sub-agent's workspace
have all moved. So the result text is written to make the model contextually
aware of where it now IS, not merely to confirm that a call succeeded: a model
that reads "ok" and keeps using paths from the previous project has been told
the truth and still gets everything wrong.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mewbo_core.common import MockSpeaker, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.workspaces.project_catalog import ProjectCatalog, ProjectEntry

logging = get_logger(name="core.project_switch")


LIST_PROJECTS_TOOL_ID = "list_projects"
SWITCH_PROJECT_TOOL_ID = "switch_project"
# ``SWITCH_PROJECT_TOOL_ID`` is the ONE home of that wire string: the transcript
# assembler reads it to decide which tool result is conversation rather than
# trace, and a duplicated literal is exactly how a stamped prefix once outlived
# every classifier able to read it. Keeping this module importable for that one
# constant is why the catalog is reached for LAZILY below rather than at module
# scope — a pure transcript reader has no business loading the project and
# repository stores (and, through them, the git worktree machinery).

# Result caps, DECLARED rather than inherited. A SessionTool has no ``ToolSpec``,
# so an undeclared cap silently falls through to the registry's 2000-char default
# — a number sized for unbounded shell/MCP output, which has twice truncated a
# curated first-party payload here badly enough that the model spent whole steps
# working around the truncation. Sized from the artifact: a catalog row is ~200
# characters of JSON, so 40,000 holds a couple of hundred projects, and a switch
# result is a handful of lines that can only grow with the length of a path.
LIST_PROJECTS_MAX_RESULT_CHARS = 40_000
SWITCH_PROJECT_MAX_RESULT_CHARS = 4_000

# The fields of a catalog entry the model is shown. ``parent_key`` is
# deliberately absent: a worktree already reports its branch, and the parent's
# key is an identifier the model can neither pass back nor act on differently.
_ENTRY_FIELDS = (
    "key",
    "name",
    "kind",
    "path",
    "description",
    "available",
    "repo",
    "branch",
)


def _error_envelope(code: str, message: str) -> MockSpeaker:
    """Structured error envelope the loop reclassifies as a FAILED step.

    The exact shape ``tool_use_loop._SessionToolError.parse`` recognises: the
    ``str(dict)`` Python repr, NOT ``json.dumps``. That asymmetry is load-bearing
    — the parse is ``ast.literal_eval`` and a JSON envelope is silently declined,
    so the tool's failure would record as a success and the loop's per-step
    failure nudge would never fire.
    """
    return MockSpeaker(content=str({"error": {"code": code, "message": message}}))


LIST_PROJECTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": LIST_PROJECTS_TOOL_ID,
        "description": (
            "List every project you can work in — operator-configured "
            "directories first, then Mewbo-managed projects and worktrees, then "
            "registered repositories. Each entry carries the 'key' you pass to "
            "switch_project, plus its kind, absolute path, description and "
            "whether the directory exists on this host right now. Call this "
            "when the request names a project you are not already in, or names "
            "no project at all and you need to pick one. An entry with "
            "'available': false has no usable directory yet and cannot be "
            "switched to."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


SWITCH_PROJECT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": SWITCH_PROJECT_TOOL_ID,
        "description": (
            "Move your working directory to a project from list_projects. This "
            "re-points EVERYTHING that follows: relative paths, shell commands, "
            "file edits, the project instructions in your system prompt, and the "
            "workspace every sub-agent you spawn afterwards inherits. Pass the "
            "'key' exactly as list_projects reported it. Switch once, before you "
            "start the work — not between individual file reads. If you are "
            "unsure which project the request means, call list_projects first "
            "rather than guessing a key."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "The catalog 'key' of the project to work in, copied "
                        "verbatim from list_projects (e.g. 'assistant' or "
                        "'managed:3f9c…')."
                    ),
                },
            },
            "required": ["project"],
        },
    },
}


class ListProjectsTool:
    """Reads the project catalog for the model — a projection, never a mutation.

    ``terminal_reason`` and ``should_terminate_run`` are defined explicitly
    because :class:`~mewbo_core.tooling.session_tools.SessionTool` is a structural
    Protocol: its default method bodies are NOT inherited by a standalone
    implementer, and a missing ``terminal_reason`` raises an ``AttributeError``
    only AFTER the tool has already done its work.
    """

    tool_id: str = LIST_PROJECTS_TOOL_ID
    schema: dict[str, object] = LIST_PROJECTS_SCHEMA
    # Choosing where to work is as much a planning concern as an acting one —
    # a plan drafted against the wrong directory is wrong in every step.
    modes: frozenset[str] = frozenset({"plan", "act"})
    max_result_chars: int = LIST_PROJECTS_MAX_RESULT_CHARS

    def __init__(self, *, catalog: ProjectCatalog) -> None:
        """Bind the injected catalog (a FIELD, so a test drives this with a fake)."""
        self.catalog = catalog

    def should_terminate_run(self) -> bool:
        """Never terminates — reading the catalog is an ordinary step."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); explicit for Protocol-default parity."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Render the catalog as JSON, preserving the catalog's own ordering."""
        del action_step  # No arguments — the catalog is the whole input.
        entries = self.catalog.entries()
        payload = {
            # ``entries()`` already leads with the operator's own configured
            # directories, so the order is carried through rather than re-sorted:
            # re-sorting here would silently become a second, divergent opinion
            # about which project a model should read first.
            "projects": [self._project_row(entry) for entry in entries],
            "count": len(entries),
        }
        return MockSpeaker(content=json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _project_row(entry: ProjectEntry) -> dict[str, object]:
        """One catalog entry reduced to the fields the model is shown."""
        return {field: getattr(entry, field) for field in _ENTRY_FIELDS}


class SwitchProjectTool:
    """Re-points the running loop at another project, then says where it now is.

    Two collaborators, both injected as FIELDS: the ``catalog`` that turns a key
    into an entry, and ``rebind`` — the loop's single mutation seam. The tool
    performs no filesystem work and holds no loop reference, which is what keeps
    the "one atomic class per feature" split honest: resolution belongs to the
    catalog, mutation belongs to the loop, and this class owns only the argument
    contract and the report the model reads back.
    """

    tool_id: str = SWITCH_PROJECT_TOOL_ID
    schema: dict[str, object] = SWITCH_PROJECT_SCHEMA
    modes: frozenset[str] = frozenset({"plan", "act"})
    max_result_chars: int = SWITCH_PROJECT_MAX_RESULT_CHARS

    def __init__(
        self,
        *,
        catalog: ProjectCatalog,
        rebind: Callable[[ProjectEntry], dict[str, object]],
    ) -> None:
        """Bind the catalog and the loop's rebind seam."""
        self.catalog = catalog
        self.rebind = rebind

    def should_terminate_run(self) -> bool:
        """Never terminates — the agent keeps working in the new directory."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); explicit for Protocol-default parity."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Resolve the key, perform the switch, and report the new workspace."""
        from mewbo_core.workspaces.project_catalog import ProjectResolutionError

        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        raw = args.get("project")
        if not isinstance(raw, str):
            return _error_envelope(
                "validation", "'project' must be a project key from list_projects."
            )
        try:
            # ``resolve`` is the authoritative refusal: it owns the empty key,
            # the 'auto' sentinel, the unknown key, the registered-but-unchecked-
            # out repository and the path that does not exist on this host, and
            # its message NAMES what is available. Mapping ``code`` straight
            # through keeps the envelope's vocabulary the catalog's, not a second
            # taxonomy this module would have to keep in step.
            self.catalog.resolve(raw)
        except ProjectResolutionError as exc:
            return _error_envelope(exc.code, exc.message)
        entry = self.catalog.find(raw)
        if entry is None:  # pragma: no cover - resolve() guarantees an entry
            return _error_envelope(
                "not_found", f"Project '{raw.strip()}' resolved but could not be read back."
            )
        try:
            info = self.rebind(entry)
        except Exception as exc:  # noqa: BLE001 - a failed switch must not kill the run
            logging.error("switch_project rebind failed for {}: {}", entry.key, exc)
            return _error_envelope(
                "rebind_failed",
                f"Could not move into '{entry.key}': {type(exc).__name__}: {exc}. "
                "You are still in the previous project.",
            )
        return MockSpeaker(
            content=json.dumps(self._result(entry, info), ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _result(entry: ProjectEntry, info: dict[str, object]) -> dict[str, object]:
        """The switch result: FIXED keys for a renderer, plus prose for the model.

        JSON rather than prose because this result has two readers with opposite
        needs. A transcript card wants stable keys; the model wants a sentence
        telling it where it now is. Prose alone would force the card to regex a
        human sentence — a shared contract expressed as a format string, which
        drifts the first time the wording is improved. So the sentence becomes
        one FIELD (``summary``) and everything else is named.

        Every key is ALWAYS present — ``null`` when unknown rather than omitted —
        so a renderer branches on values, never on key existence.

        ``cwd`` (not ``path``) is deliberate: it is the name this tree uses
        everywhere for the directory a session works in, and it matches the
        ``context`` event the switch writes. The catalog rows in
        ``list_projects`` keep ``path`` because those describe ENTRIES, one kind
        of which (a registered repository with no checkout) legitimately has
        none — a different fact from "where the agent is now".

        ``previous_project`` is ``null`` on the FIRST switch of a session and
        that is honest, not a gap to work around: the loop is handed a
        directory at construction, never a catalog key, so before the first
        switch there is no key to report. ``previous_cwd`` is always known —
        render that when the key is absent.
        """
        cwd = info.get("cwd") or entry.path
        summary = [
            f"Switched to project '{entry.key}' ({entry.kind}).",
            f"Working directory: {cwd}",
        ]
        if entry.repo:
            summary.append(f"Repository: {entry.repo}")
        if entry.branch:
            summary.append(f"Branch: {entry.branch}")
        if entry.description:
            summary.append(f"Description: {entry.description}")
        if info.get("project_instructions_found"):
            summary.append(
                "Project instructions: found and loaded into your system prompt — "
                "follow them for the rest of this session."
            )
        else:
            summary.append("Project instructions: none found in this directory.")
        summary.append(
            f"Tools: {info.get('bound_tools', 0)} bound for this workspace; "
            f"{info.get('skills', 0)} skill(s) discovered here."
        )
        # The closing line is the point of the whole result. A confirmation the
        # model reads as "the call worked" leaves it free to keep using paths
        # from the project it just left, which fails silently; naming what moved
        # is what makes the switch legible where the model is already looking.
        summary.append(
            "From now on every relative path, shell command and file edit "
            "resolves against this directory, and every sub-agent you spawn "
            "starts here."
        )
        return {
            "project": entry.key,
            "name": entry.name,
            "kind": entry.kind,
            "cwd": cwd,
            "repo": entry.repo,
            "branch": entry.branch,
            "description": entry.description,
            "previous_project": info.get("previous_project"),
            "previous_cwd": info.get("previous_cwd"),
            "project_instructions_found": bool(info.get("project_instructions_found")),
            "bound_tools": info.get("bound_tools", 0),
            "skills": info.get("skills", 0),
            "summary": "\n".join(summary),
        }


__all__ = [
    "LIST_PROJECTS_MAX_RESULT_CHARS",
    "LIST_PROJECTS_SCHEMA",
    "LIST_PROJECTS_TOOL_ID",
    "SWITCH_PROJECT_MAX_RESULT_CHARS",
    "SWITCH_PROJECT_SCHEMA",
    "SWITCH_PROJECT_TOOL_ID",
    "ListProjectsTool",
    "SwitchProjectTool",
]
