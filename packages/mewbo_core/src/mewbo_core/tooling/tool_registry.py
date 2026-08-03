#!/usr/bin/env python3
"""Tool registry and manifest loading for Mewbo."""

from __future__ import annotations

import importlib
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal, Protocol

from mewbo_core.classes import ActionStep, set_available_tools
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.components import ComponentStatus, resolve_home_assistant_status
from mewbo_core.config import get_config_value, get_mcp_config_path
from mewbo_core.contracts.types import JsonValue
from mewbo_core.tooling.tool_schema import ToolParameter, ToolSchema

logging = get_logger(name="core.tool_registry")

# Built-in tool ID for the on-demand schema fetcher. MCP tools (and any
# spec with metadata.deferred=True) are stripped from the initial bind
# and discovered through this tool when ``agent.tool_search.mode == "on"``.
TOOL_SEARCH_TOOL_ID = "tool_search"

# Coarse delegation privilege tier — privilege attenuation. A spawn declares
# the ceiling its sub-tree may reach; it is a pre-filter LAYERED UNDER
# ``allowed_tools``/``denied_tools`` — it can only remove more tools, never
# resurrect one the allow/deny gates dropped. The three tiers form a monotone
# chain ``read_only`` ⊆ ``execute`` ⊆ ``all``:
#   * ``read_only`` — only tools whose declared capability is ``read`` survive
#     (plus ``always_load``/``tool_search``, harmless discovery a scoped child
#     must keep).
#   * ``execute`` — every DECLARED tool survives (read/write/execute); an
#     undeclared tool is withheld (safe-deny — undeclared ≠ safe).
#   * ``all`` — no capability filtering (the unrestricted default).
CapabilityMode = Literal["read_only", "execute", "all"]

# The tool-capability tiers each mode admits. ``all`` (and any unrecognised
# string — the authoritative validation is the ``Literal`` at the
# ``SpawnAgentTask`` boundary) has NO entry, so ``.get()`` returns ``None`` and
# the capability gate is skipped entirely. Mirrors the ordering encoded in
# ``AgentContext._CAPABILITY_MODE_RANK``; the two stay consistent by a test.
_CAPABILITY_MODE_TIERS: dict[str, frozenset[str]] = {
    "read_only": frozenset({"read"}),
    "execute": frozenset({"read", "write", "execute"}),
}


def capability_mode_admits(capability_mode: str, tier: str | None) -> bool:
    """True if a tool of privilege *tier* survives *capability_mode*.

    The ONE home of the mode→tier law, shared by the registry filter
    (:func:`filter_specs`) AND the session-tool build
    (``SessionToolRegistry.ids_for``) so the two enforcement surfaces can never
    drift (the two-surface trap). ``all`` — and any unrecognised mode, since
    the authoritative validation is the ``Literal`` at the ``SpawnAgentTask``
    boundary — admits everything (the gate is skipped). An undeclared tier
    (``None``) is admitted ONLY by ``all``: safe-deny under any restrictive mode.
    """
    allowed_tiers = _CAPABILITY_MODE_TIERS.get(capability_mode)
    if allowed_tiers is None:
        return True
    return tier in allowed_tiers


def _load_mcp_support():
    try:
        from mewbo_tools.integration import mcp as mcp_module
    except Exception as exc:  # pragma: no cover - optional dependency
        logging.debug("MCP support unavailable: {}", exc)
        return None
    return mcp_module


class ToolRunner(Protocol):
    def run(self, action_step: ActionStep) -> MockSpeaker:  # pragma: no cover
        """Execute an action step and return a speaker response.

        Args:
            action_step: Action step payload to execute.

        Returns:
            MockSpeaker response from the tool.
        """


@dataclass(frozen=True)
class ToolSpec:
    """Metadata describing a tool available to the assistant."""

    tool_id: str
    name: str
    description: str
    factory: Callable[[], ToolRunner]
    enabled: bool = True
    kind: str = "local"
    prompt_path: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    concurrency_safe: bool = True  # Can run in parallel (True for backward compat)
    read_only: bool = False  # No side effects
    interrupt_behavior: str = "block"  # "cancel" or "block" on user interrupt
    max_result_chars: int = 2000  # Per-tool result size cap (0 = unlimited)
    timeout: float = 120.0  # Per-tool execution timeout in seconds
    # POLL-CLASS: this tool's documented contract is "call me again until the
    # thing I front settles" (a nested run's status probe, a wait primitive).
    # Repeated identical calls to it are honest waiting, so ``DoomLoopGuard``
    # drops them from the no-progress signature. Declared HERE rather than as
    # another hardcoded id in ``llm_resilience``: two sub-second "processing"
    # answers from a self-polling run are indistinguishable from a stuck loop
    # by repetition alone, and only the tool knows which it is.
    #
    # ``poll`` exempts EVERY call (a pure wait primitive). ``poll_when_args``
    # exempts only calls carrying one of the named arguments, which is what a
    # tool that both STARTS and POLLS the same work needs — one tool id, two
    # meanings, told apart by argument shape alone. Exempting such an id
    # outright would blind the guard to an agent re-issuing the same start
    # forever.
    poll: bool = False
    poll_when_args: tuple[str, ...] = ()
    # Declared privilege tier for delegation ``capability_mode`` filtering
    #. ``None`` = undeclared (treated as NOT read — safe-deny).
    # Distinct from ``metadata["capabilities"]`` (a permission-subsystem tag):
    # this is the coarse read/write/execute privilege a spawn's
    # ``capability_mode`` gates on. A ``read_only`` tool needs no explicit
    # value — ``capability_tier`` reads it as ``read`` — so only write/execute
    # tools declare one here.
    capability: Literal["read", "write", "execute"] | None = None

    # The knobs a BUILT-IN registration DECLARES and a manifest entry merely
    # CACHES. Each has a class default that is a decision made by silence — so
    # an entry simply LACKING the key is indistinguishable from one deliberately
    # choosing the default, and reads as the default either way.
    #
    # Deliberately excluded: identity (``tool_id``/``name``/``prompt_path``),
    # ``description`` (the two sides word one tool differently on purpose),
    # ``kind``/``metadata`` (the manifest carries discovery's schema), and
    # ``enabled`` — a manifest records a tool discovery found UNREACHABLE, and
    # overlaying that would resurrect it.
    DECLARED_KNOBS: ClassVar[tuple[str, ...]] = (
        "max_result_chars",
        "timeout",
        "concurrency_safe",
        "interrupt_behavior",
        "poll",
        "poll_when_args",
        "read_only",
        "capability",
    )

    def knob_mismatches(self, declared: ToolSpec) -> dict[str, tuple[object, object]]:
        """Fields where THIS spec disagrees with its *declared* counterpart.

        Maps each differing field to ``(mine, declared)``. Pure comparison, no
        I/O — the two specs arrive as values so a caller can compare a
        manifest-loaded spec against the built-in registration that authored it.

        This exists because the defect class is *cached data outliving the
        declaration*, which nothing else can see: a manifest written before a
        field existed carries no key for it, the reader defaults it, and the
        result is a spec that is internally consistent, loads clean, and is
        wrong. Only a comparison against the declaration catches that.
        """
        return {
            name: (mine, theirs)
            for name in self.DECLARED_KNOBS
            if (mine := getattr(self, name)) != (theirs := getattr(declared, name))
        }

    def with_declared_knobs(self, declared: ToolSpec) -> ToolSpec:
        """Return this spec with *declared*'s knob values overlaid.

        The built-in registration is the AUTHORITY for these fields and the
        manifest entry is a cache of it, so the declaration wins — which is what
        lets an already-written manifest self-heal on the next load with no
        operator step and no regeneration. Everything else on the manifest entry
        (identity, schema, enablement, MCP wiring) is untouched.
        """
        return replace(
            self, **{name: getattr(declared, name) for name in self.DECLARED_KNOBS}
        )

    def capability_tier(self) -> str | None:
        """Resolve this tool's privilege tier for ``capability_mode`` filtering.

        Returns ``"read"`` / ``"write"`` / ``"execute"``, or ``None`` when the
        tool makes no declaration. A ``read_only`` tool is read-tier by
        construction (no side effects), so the fallback keeps ``read_only`` and
        ``capability`` in lockstep — a new read-only tool is correctly admitted
        under ``read_only`` mode without a second annotation, and the failure
        mode for a *forgotten* declaration is safe-deny, not silent grant.
        ``None`` is deliberately NOT treated as read: an undeclared tool is
        withheld from a ``read_only`` child.
        """
        if self.capability is not None:
            return self.capability
        if self.read_only:
            return "read"
        return None


class ToolRegistry:
    """Registry of configured tools and their instantiated runners."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._tools: dict[str, ToolSpec] = {}
        self._instances: dict[str, ToolRunner] = {}

    def disable(self, tool_id: str, reason: str) -> None:
        """Disable a tool and store a reason for later reporting."""
        spec = self._tools.get(tool_id)
        if spec is None:
            return
        metadata = dict(spec.metadata)
        metadata["disabled_reason"] = reason
        self._tools[tool_id] = ToolSpec(
            tool_id=spec.tool_id,
            name=spec.name,
            description=spec.description,
            factory=spec.factory,
            enabled=False,
            kind=spec.kind,
            prompt_path=spec.prompt_path,
            metadata=metadata,
            concurrency_safe=spec.concurrency_safe,
            read_only=spec.read_only,
            interrupt_behavior=spec.interrupt_behavior,
            max_result_chars=spec.max_result_chars,
            timeout=spec.timeout,
            capability=spec.capability,
            poll=spec.poll,
            poll_when_args=spec.poll_when_args,
        )
        if tool_id in self._instances:
            self._instances.pop(tool_id, None)
        set_available_tools(
            [current_id for current_id, current_spec in self._tools.items() if current_spec.enabled]
        )

    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification and update action validation."""
        self._tools[spec.tool_id] = spec
        set_available_tools(
            [tool_id for tool_id, tool_spec in self._tools.items() if tool_spec.enabled]
        )

    def get(self, tool_id: str) -> ToolRunner | None:
        """Return an enabled tool runner, instantiating it if needed."""
        spec = self._tools.get(tool_id)
        if spec is None or not spec.enabled:
            return None
        if tool_id not in self._instances:
            try:
                self._instances[tool_id] = spec.factory()
            except Exception as exc:  # pragma: no cover - defensive
                reason = f"Initialization failed: {exc}"
                logging.warning("Disabling tool {}: {}", tool_id, reason)
                self.disable(tool_id, reason)
                return None
        return self._instances[tool_id]

    def get_spec(self, tool_id: str) -> ToolSpec | None:
        """Return the tool specification, even if disabled."""
        return self._tools.get(tool_id)

    def list_specs(self, include_disabled: bool = False) -> list[ToolSpec]:
        """List tool specifications, optionally including disabled tools."""
        specs = list(self._tools.values())
        if include_disabled:
            return specs
        return [spec for spec in specs if spec.enabled]

    def tool_catalog(self) -> list[dict[str, str]]:
        """Return a serialized catalog of registered tool metadata."""
        return [
            {
                "tool_id": spec.tool_id,
                "name": spec.name,
                "description": spec.description,
            }
            for spec in self.list_specs()
        ]


# Argument contracts declared ONCE and rendered at both consumption sites — the
# ``ToolSpec`` below and the cached manifest entry in
# ``_built_in_manifest_entries``, which is written before those specs exist and
# therefore cannot read them. Every other built-in still spells its schema out
# as a literal in both places; migrating them is a separate change, because it
# touches the startup path this file guards and does not belong in a diff that
# also adds a feature.
SHELL_SCHEMA = ToolSchema(
    properties={
        "command": ToolParameter(type="string", description="Shell command to execute"),
        "cwd": ToolParameter(type="string", description="Working directory"),
        "timeout": ToolParameter(
            type="number",
            description=(
                "Seconds to wait before a foreground command is killed. Ignored "
                "when run_in_background is true. Raise it for a slow build; to go "
                "beyond it, background the command instead of waiting longer."
            ),
            default=115,
            minimum=1,
        ),
        "run_in_background": ToolParameter(
            type="boolean",
            description=(
                "Return immediately with a shell_id instead of waiting. Use for "
                "servers, watchers, and anything longer than the timeout; read its "
                "output with shell_session_tool. A foreground command that times "
                "out is killed and its work is lost."
            ),
            default=False,
        ),
        "tty": ToolParameter(
            type="boolean",
            description=(
                "Allocate a pseudo-terminal so the command can be driven through "
                "interactive prompts with shell_session_tool write. Only ask for "
                "it when you intend to answer the program; without it there is no "
                "terminal, which is what stops pagers and credential prompts from "
                "hanging."
            ),
            default=False,
        ),
    },
    required=("command",),
)

SHELL_SESSION_SCHEMA = ToolSchema(
    properties={
        "operation": ToolParameter(
            type="string",
            description="What to do with the session.",
            enum=("read", "write", "kill", "list"),
        ),
        "shell_id": ToolParameter(
            type="string", description="Handle returned by run_in_background."
        ),
        "input": ToolParameter(
            type="string",
            description=(
                "Text to send to the program's stdin (operation=write). The reply "
                "is read back in the same call, so answering a prompt costs one "
                "step. Writing to a session that has already exited is refused."
            ),
        ),
        "newline": ToolParameter(
            type="boolean",
            description=(
                "Append a newline to the input, which is what submits it to most "
                "programs. Set false to send a bare keystroke."
            ),
            default=True,
        ),
        "cursor": ToolParameter(
            type="integer",
            description=(
                "Read only output produced after this cursor. Pass back the cursor "
                "from your previous read to get just what is new; omit it to get "
                "everything retained. A missed_characters field in the reply means "
                "output was evicted before you read it and is unrecoverable."
            ),
            minimum=0,
        ),
        "filter": ToolParameter(
            type="string",
            description=(
                "Regular expression; only matching lines are shown. Display-only — "
                "it never consumes output, so the cursor advances exactly as it "
                "would on an unfiltered read."
            ),
        ),
        "wait_ms": ToolParameter(
            type="integer",
            description=(
                "Wait up to this many milliseconds for new output before returning, "
                "instead of returning an empty read immediately. Use it when you "
                "expect output shortly; it saves a whole polling step."
            ),
            default=0,
            minimum=0,
            maximum=30000,
        ),
    },
    required=("operation",),
)


def _import_factory(module_path: str, class_name: str) -> Callable[[], ToolRunner]:
    """Return a factory that instantiates a tool by import path."""

    def _factory() -> ToolRunner:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls()

    return _factory


def is_always_load(spec: ToolSpec) -> bool:
    """Return True if the tool's full schema must always be in the bound list.

    Marked via ``metadata.always_load=True``. An opt-out from deferral —
    used by tools that the model needs immediately
    (the search tool itself, or any tool whose absence would block the
    model from making progress).
    """
    return bool(spec.metadata.get("always_load"))


def is_deferred(spec: ToolSpec) -> bool:
    """Return True if the tool's schema should be omitted from the initial bind.

    Deferred tools surface as names only via ``<available-deferred-tools>`` —
    the model fetches their schemas on demand via ``tool_search``. The
    deferral rule: ``always_load`` wins, the search tool
    itself never defers, all MCP tools defer, and other tools opt-in via
    ``metadata.deferred=True``.
    """
    if is_always_load(spec):
        return False
    if spec.tool_id == TOOL_SEARCH_TOOL_ID:
        return False
    if spec.kind == "mcp":
        return True
    return bool(spec.metadata.get("deferred"))


def classify_tool_scope(
    spec: ToolSpec, *, global_servers: set[str], plugin_servers: set[str]
) -> str:
    """Classify a tool spec into its deployment scope.

    The four real scope categories:

    - ``builtin`` — a core built-in Python tool (``spec.kind != "mcp"``),
      not an MCP server at all.
    - ``system`` — an MCP tool whose server is configured in the shared,
      deployed-instance ``mcp.json`` (``global_servers``).
    - ``plugin`` — an MCP tool contributed by an installed plugin
      (``plugin_servers``).
    - ``project`` — an MCP tool whose server is configured only in the
      current project's local MCP config (neither of the above).

    A genuine ``user`` tier — a personal ``~/.mewbo`` config distinct from
    the deployed ``$MEWBO_HOME`` system instance — is deliberately NOT
    implemented: no current infra distinguishes the two in a way worth
    surfacing yet.
    """
    if spec.kind != "mcp":
        return "builtin"
    server = spec.metadata.get("server", "")
    if server in plugin_servers:
        return "plugin"
    if server in global_servers:
        return "system"
    return "project"


_TOOL_SEARCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Use 'select:tool_a,tool_b' for direct fetch by name, "
                "or keywords for fuzzy search. Prefix a term with '+' to "
                "require it (e.g. '+linear issues')."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return (default 5).",
        },
    },
    "required": ["query"],
}


_FILE_EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "Path to the file to edit"},
        "old_string": {"type": "string", "description": "Exact string to find in the file"},
        "new_string": {"type": "string", "description": "Replacement string"},
        "replace_all": {
            "type": "boolean",
            "description": "Replace all occurrences (default false)",
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}

_AIDER_EDIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "SEARCH/REPLACE block content"},
        "root": {"type": "string", "description": "Project root directory"},
    },
    "required": ["content"],
}


def _all_edit_tool_specs() -> list[ToolSpec]:
    """Return ToolSpecs for both edit tools.

    Both are always registered; ``ToolUseLoop._configured_edit_tool_id()``
    selects the active one per-model at schema-building time.
    """
    file_edit_spec = ToolSpec(
        tool_id="file_edit_tool",
        name="File Edit",
        description="Apply exact string replacement to a file.",
        factory=_import_factory("mewbo_tools.integration.file_edit_tool", "FileEditTool"),
        prompt_path="tools/file-edit",
        concurrency_safe=False,
        capability="write",
        metadata={
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _FILE_EDIT_SCHEMA,
        },
    )
    aider_edit_spec = ToolSpec(
        tool_id="aider_edit_block_tool",
        name="Aider Edit Blocks",
        description="Apply Aider-style SEARCH/REPLACE blocks to files.",
        factory=_import_factory("mewbo_tools.integration.aider_edit_blocks", "AiderEditBlockTool"),
        prompt_path="tools/aider-edit-blocks",
        concurrency_safe=False,
        capability="write",
        metadata={
            "reflect": True,
            "capabilities": ["file_write"],
            "schema": _AIDER_EDIT_SCHEMA,
        },
    )
    return [file_edit_spec, aider_edit_spec]


def _all_edit_tool_manifest_entries() -> list[dict[str, object]]:
    """Return manifest entries for both edit tools."""
    return [
        {
            "tool_id": "file_edit_tool",
            "name": "File Edit",
            "description": "Apply exact string replacement to a file.",
            "module": "mewbo_tools.integration.file_edit_tool",
            "class": "FileEditTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/file-edit",
            "reflect": True,
            # Mirrors the direct registration. Omitted, this defaulted to True
            # and every manifest-backed deployment ran the edit tools in
            # PARALLEL with each other and with the shell — the field was as
            # dead as the result caps, and for the same reason.
            "concurrency_safe": False,
            "capability": "write",
            "capabilities": ["file_write"],
            "schema": _FILE_EDIT_SCHEMA,
        },
        {
            "tool_id": "aider_edit_block_tool",
            "name": "Aider Edit Blocks",
            "description": "Apply Aider-style SEARCH/REPLACE blocks to files.",
            "module": "mewbo_tools.integration.aider_edit_blocks",
            "class": "AiderEditBlockTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-edit-blocks",
            "reflect": True,
            "concurrency_safe": False,
            "capability": "write",
            "capabilities": ["file_write"],
            "schema": _AIDER_EDIT_SCHEMA,
        },
    ]


def _resolve_lsp_status() -> ComponentStatus:
    """Determine whether the LSP tool should be enabled."""
    from mewbo_core.config import get_config

    cfg = get_config()
    lsp_cfg = cfg.agent.lsp
    if not lsp_cfg.enabled:
        return ComponentStatus(name="lsp_tool", enabled=False, reason="disabled via config")
    try:
        from mewbo_tools.integration.lsp import LSP_AVAILABLE

        if not LSP_AVAILABLE:
            return ComponentStatus(
                name="lsp_tool",
                enabled=False,
                reason="pygls not installed",
            )
        from mewbo_tools.integration.lsp.servers import available_servers

        servers = available_servers(lsp_cfg.servers)
        if not servers:
            return ComponentStatus(
                name="lsp_tool",
                enabled=False,
                reason="no language server binaries found on PATH",
            )
    except Exception as exc:
        return ComponentStatus(name="lsp_tool", enabled=False, reason=str(exc))
    return ComponentStatus(name="lsp_tool", enabled=True)


def _default_registry() -> ToolRegistry:
    """Create the built-in registry for local tools."""
    registry = ToolRegistry()
    ha_status = resolve_home_assistant_status()
    ha_metadata: dict[str, JsonValue] = {
        "schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task to perform"},
            },
            "required": ["task"],
        },
    }
    if not ha_status.enabled:
        ha_metadata["disabled_reason"] = ha_status.reason
    registry.register(
        ToolSpec(
            tool_id="home_assistant_tool",
            name="Home Assistant",
            description="Manage smart home devices via Home Assistant.",
            factory=_import_factory(
                "mewbo_tools.integration.homeassistant",
                "HomeAssistant",
            ),
            enabled=ha_status.enabled,
            prompt_path="tools/home-assistant",
            capability="execute",
            # A state query over a whole home returns dozens of entities with
            # their attributes, and the tool offers NO paging affordance — so a
            # cut is unrecoverable rather than resumable, unlike a windowed file
            # read. 30_000 is this file's convergent figure for a bulk payload
            # (both shell tools); it covers a full-home dump without inviting
            # one to be pasted whole into every subsequent turn.
            max_result_chars=30_000,
            metadata=ha_metadata,
        )
    )
    for edit_spec in _all_edit_tool_specs():
        registry.register(edit_spec)
    registry.register(
        ToolSpec(
            tool_id="read_file",
            name="Read File",
            description=(
                "Read local files. Reads are line-windowed: pass offset/limit "
                "to page through a file larger than the default window."
            ),
            factory=_import_factory(
                "mewbo_tools.integration.aider_file_tools",
                "ReadFileTool",
            ),
            prompt_path="tools/read-file",
            read_only=True,
            # The registry's 2000-CHAR class default is two orders of
            # magnitude tighter than this tool's own 2000-LINE window — left
            # undeclared, the tighter one wins silently and a model reading
            # the schema's "2000 lines" promise gets ~20-40 lines back with
            # no honest signal beyond an inline omission marker. Match the
            # session-tool default (`DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS`,
            # `wiki_read_file`'s own byte cap) so the loop's cap stops being
            # the tighter of the two.
            max_result_chars=200_000,
            metadata={
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to read"},
                        "root": {"type": "string", "description": "Project root"},
                        "offset": {
                            "type": "integer",
                            "description": "Line to start from (0-based). For large files.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max lines to read. Defaults to 2000.",
                        },
                    },
                    "required": ["path"],
                },
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="aider_list_dir_tool",
            name="Aider List Directory",
            description="List files under a directory using Aider helpers.",
            factory=_import_factory(
                "mewbo_tools.integration.aider_file_tools",
                "AiderListDirTool",
            ),
            prompt_path="tools/aider-list-dir",
            read_only=True,
            # A listing is a BULK payload: one repo-relative path per entry,
            # thousands of them on a real checkout, and the 2000-char default
            # delivered roughly the first 45 of them. Unlike `read_file` there
            # is no offset/limit to resume from — `max_entries` truncates from
            # the same end — so what the cut removes cannot be asked for again.
            max_result_chars=30_000,
            metadata={
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to list"},
                    },
                    "required": ["path"],
                },
            },
        )
    )
    registry.register(
        ToolSpec(
            tool_id="aider_shell_tool",
            name="Aider Shell",
            description="Run shell commands using Aider helpers.",
            factory=_import_factory(
                "mewbo_tools.integration.aider_shell_tool",
                "AiderShellTool",
            ),
            prompt_path="tools/aider-shell",
            concurrency_safe=False,
            capability="execute",
            # Shell output is the archetypal unbounded payload, and the 2000-char
            # registry default was never sized for it: a test run, a build log or
            # a paged API response reached the model as its first 2000 characters
            # — which is the END a traceback is NOT at. That cap is why so much
            # tooling here pipes through head/tail/grep before the model ever
            # sees anything; the pre-filtering was a workaround for the cap, not
            # a preference. 30_000 is the convergent figure across the field
            # (Claude Code's default max output, qwen-code's maxOutputChars).
            max_result_chars=30_000,
            metadata={"reflect": True, "schema": SHELL_SCHEMA.as_json_schema()},
        )
    )
    registry.register(
        ToolSpec(
            tool_id="shell_session_tool",
            name="Shell Session",
            description="Read output from, write input to, or stop a background shell.",
            factory=_import_factory(
                "mewbo_tools.integration.shell_session_tool",
                "ShellSessionTool",
            ),
            prompt_path="tools/shell-session",
            concurrency_safe=False,
            capability="execute",
            max_result_chars=30_000,
            # Reading a background shell until it settles is honest waiting, not
            # a stuck loop — the same distinction ``agentic_search(run_id=…)``
            # draws. ``list`` carries no ``shell_id`` and so still counts, which
            # is correct: re-listing forever IS non-progress.
            poll_when_args=("shell_id",),
            metadata={"schema": SHELL_SESSION_SCHEMA.as_json_schema()},
        )
    )
    # LSP tool — opt-in, requires pygls + at least one server binary
    lsp_status = _resolve_lsp_status()
    lsp_meta: dict[str, JsonValue] = {
        "schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["diagnostics", "definition", "references", "hover"],
                    "description": "LSP operation to perform",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file",
                },
                "line": {
                    "type": "integer",
                    "description": "0-based line number (for definition/references/hover)",
                },
                "character": {
                    "type": "integer",
                    "description": "0-based column (for definition/references/hover)",
                },
            },
            "required": ["operation", "file_path"],
        },
    }
    if not lsp_status.enabled:
        lsp_meta["disabled_reason"] = lsp_status.reason
    registry.register(
        ToolSpec(
            tool_id="lsp_tool",
            name="Language Server",
            description=(
                "Query language servers for code diagnostics, "
                "go-to-definition, find-references, and hover info."
            ),
            factory=_import_factory(
                "mewbo_tools.integration.lsp.tool",
                "LSPTool",
            ),
            enabled=lsp_status.enabled,
            prompt_path="tools/lsp",
            read_only=True,
            concurrency_safe=True,
            metadata=lsp_meta,
        )
    )
    _register_tool_search(registry)
    return registry


def _register_tool_search(registry: ToolRegistry) -> None:
    """Register the on-demand schema-fetching tool against ``registry``.

    Bound last so the runner's factory closes over the registry instance
    that holds every other spec — including any MCP entries merged in by
    ``load_registry``. Always-loaded itself (``metadata.always_load``) so
    the model can reach it on turn one.
    """

    def _factory() -> ToolRunner:
        from mewbo_tools.integration.tool_search import ToolSearchRunner

        return ToolSearchRunner(registry)

    registry.register(
        ToolSpec(
            tool_id=TOOL_SEARCH_TOOL_ID,
            name="Tool Search",
            description=(
                "Fetch full JSON schemas for deferred tools so they can be "
                "called. Use 'select:name1,name2' for direct fetch or "
                "keywords for fuzzy search."
            ),
            factory=_factory,
            # No ``prompt_path``: there is no ``prompts/tools/tool-search.txt``,
            # and both consumers swallow the miss (``planning.py`` logs a warning
            # per render, ``_render_tool_guidance`` skips silently) — so the
            # declaration bought nothing but noise. This tool's usage contract
            # lives in its schema description instead.
            read_only=True,
            concurrency_safe=True,
            metadata={"schema": _TOOL_SEARCH_SCHEMA, "always_load": True},
        )
    )


def _default_manifest_cache_path() -> str:
    base_dir = get_config_value("runtime", "config_dir")
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), ".mewbo")
    base_dir = os.path.expanduser(str(base_dir))
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "tool-manifest.auto.json")


def _sanitize_tool_id(server_name: str, tool_name: str) -> str:
    """Build the internal ``mcp_<server>_<tool>`` tool id.

    Some MCP servers (searxng, deepwiki, …) emit tool names that already
    embed the server name (e.g. server ``internet-search`` exposes
    ``Internet-Search-searxng_web_search``). Concatenating naively yields
    duplicate-prefix ids like ``mcp_internet_search_internet_search_…``,
    which leak into Langfuse traces, the picker, and skill allowlists.
    Strip the redundant prefix once both sides have been normalised so
    the id stays ``mcp_<server>_<tool>``.
    """
    server_norm = re.sub(r"[^a-z0-9_]+", "_", server_name.lower()).strip("_")
    tool_norm = re.sub(r"[^a-z0-9_]+", "_", tool_name.lower()).strip("_")
    if server_norm and tool_norm.startswith(f"{server_norm}_"):
        tool_norm = tool_norm[len(server_norm) + 1 :]
    raw = f"mcp_{server_norm}_{tool_norm}" if tool_norm else f"mcp_{server_norm}"
    return re.sub(r"_+", "_", raw).strip("_")


def mcp_tool_id(server_name: str, tool_name: str) -> str:
    """Public alias for the canonical ``mcp_<server>_<tool>`` id convention.

    Anything that must NAME an executable MCP tool outside the registry (the
    SCG route projection emitting a probe's ``allowed_tools``, allowlist
    builders, trace labels) must derive the id HERE — never by string-mangling
    a ``source_key``. A graph ``source_key`` (``<source>#<Capability>``) is a
    graph address, not a tool id; passing it to ``allowed_tools`` silently
    grants nothing (the run-c52e9597 probe failure).
    """
    return _sanitize_tool_id(server_name, tool_name)


def _built_in_manifest_entries() -> list[dict[str, object]]:
    ha_status = resolve_home_assistant_status()
    entries: list[dict[str, object]] = [
        {
            "tool_id": "home_assistant_tool",
            "name": "Home Assistant",
            "description": "Manage smart home devices via Home Assistant.",
            "module": "mewbo_tools.integration.homeassistant",
            "class": "HomeAssistant",
            "kind": "local",
            "enabled": ha_status.enabled,
            "prompt": "tools/home-assistant",
            "capability": "execute",
            # Mirrors the direct registration; the load-time overlay repairs a
            # manifest that predates this key, but the WRITER must still emit it
            # so a freshly written file needs no repair.
            "max_result_chars": 30_000,
            "schema": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Task to perform"}},
                "required": ["task"],
            },
        },
        *_all_edit_tool_manifest_entries(),
        {
            "tool_id": "read_file",
            "name": "Read File",
            "description": (
                "Read a local file. Returns numbered lines plus the file's total "
                "line count. Reads are windowed to 2000 lines by default; page "
                "through a longer file with offset and limit."
            ),
            "module": "mewbo_tools.integration.aider_file_tools",
            "class": "ReadFileTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/read-file",
            # Mirrors the direct registration. These two declarations describe
            # ONE tool and must not disagree — the manifest is what a deployment
            # with an MCP config actually loads, so a cap declared only on the
            # other side is dead exactly where it is needed.
            "max_result_chars": 200_000,
            "read_only": True,
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "root": {"type": "string", "description": "Project root"},
                    "offset": {
                        "type": "integer",
                        "description": "Line to start from (0-based). For large files.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read. Defaults to 2000.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "tool_id": "aider_list_dir_tool",
            "name": "Aider List Directory",
            "description": "List files under a directory using Aider helpers.",
            "module": "mewbo_tools.integration.aider_file_tools",
            "class": "AiderListDirTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-list-dir",
            "read_only": True,
            "max_result_chars": 30_000,
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"},
                },
                "required": ["path"],
            },
        },
        {
            "tool_id": "aider_shell_tool",
            "name": "Aider Shell",
            "description": "Run shell commands using Aider helpers.",
            "module": "mewbo_tools.integration.aider_shell_tool",
            "class": "AiderShellTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/aider-shell",
            "reflect": True,
            "concurrency_safe": False,
            "capability": "execute",
            "max_result_chars": 30_000,
            "schema": SHELL_SCHEMA.as_json_schema(),
        },
        {
            "tool_id": "shell_session_tool",
            "name": "Shell Session",
            "description": "Read output from, write input to, or stop a background shell.",
            "module": "mewbo_tools.integration.shell_session_tool",
            "class": "ShellSessionTool",
            "kind": "local",
            "enabled": True,
            "prompt": "tools/shell-session",
            "concurrency_safe": False,
            "capability": "execute",
            "max_result_chars": 30_000,
            # Mirrors the direct registration's doom-loop poll exemption:
            # omitted, reading a background shell until it settles counted as
            # non-progress on exactly the deployments that load a manifest.
            "poll_when_args": ["shell_id"],
            "schema": SHELL_SESSION_SCHEMA.as_json_schema(),
        },
    ]
    if not ha_status.enabled and ha_status.reason:
        entries[0]["disabled_reason"] = ha_status.reason
    return entries


def _build_manifest_payload(
    mcp_tools: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    tools: list[dict[str, object]] = _built_in_manifest_entries()
    for server_name, tool_specs in mcp_tools.items():
        for tool_spec in tool_specs:
            tool_name = str(tool_spec.get("name", "")).strip()
            if not tool_name:
                continue
            tools.append(
                {
                    "tool_id": _sanitize_tool_id(server_name, tool_name),
                    "name": tool_name,
                    "description": f"MCP tool `{tool_name}` from `{server_name}`.",
                    "kind": "mcp",
                    "server": server_name,
                    "tool": tool_name,
                    "enabled": True,
                    "schema": tool_spec.get("schema"),
                }
            )
    return {"tools": tools}


def _resolve_mcp_config(
    mcp_config_path: str,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
    trust_cwd: bool = True,
) -> dict[str, object] | None:
    """Return the normalized, merged MCP config used for discovery + hashing.

    Centralizes the global-vs-merged-vs-extra-servers branch so the startup
    config hash (the non-blocking gate) is computed against the
    exact same config the discovery path would connect with. Returns ``None``
    when MCP support is unavailable or the config can't be read.

    *trust_cwd* rides through to ``get_merged_mcp_config``; ``False`` drops the
    ``cwd``/subtree tiers, which is what keeps a directory this deployment did
    not author from naming a process to spawn.
    """
    try:
        from mewbo_tools.integration.mcp import _normalize_mcp_config

        if extra_mcp_servers:
            from mewbo_core.config import get_merged_mcp_config

            return _normalize_mcp_config(
                get_merged_mcp_config(cwd, extra_servers=extra_mcp_servers, trust_cwd=trust_cwd)
            )
        from mewbo_tools.integration.mcp import _load_mcp_config

        return _normalize_mcp_config(
            _load_mcp_config(mcp_config_path or None, cwd=cwd, trust_cwd=trust_cwd)
        )
    except Exception as exc:
        logging.debug("Could not resolve MCP config for hashing: {}", exc)
        return None


def _config_fingerprint(config: dict[str, object] | None) -> str | None:
    """Stable hash of the resolved MCP config (reuses the pool's hasher)."""
    if config is None:
        return None
    try:
        from mewbo_tools.integration.mcp_pool import _config_hash

        return _config_hash(config)
    except Exception:
        return None


def _manifest_has_mcp_tools(manifest: dict[str, JsonValue]) -> bool:
    """True when a cached manifest already carries at least one MCP tool."""
    tools = manifest.get("tools", [])
    if not isinstance(tools, list):
        return False
    return any(isinstance(t, dict) and t.get("kind") == "mcp" for t in tools)


def _try_pool_discovery(
    mcp_config_path: str,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
    trust_cwd: bool = True,
) -> dict[str, list[dict[str, object]]] | None:
    """Attempt MCP tool discovery via the connection pool.

    Returns the tool details dict on success, or ``None`` if the pool
    path is unavailable or fails.
    """
    try:
        from mewbo_tools.integration.mcp import _normalize_mcp_config
        from mewbo_tools.integration.mcp_pool import get_mcp_pool
    except Exception:
        return None

    try:
        import asyncio

        if extra_mcp_servers:
            from mewbo_core.config import get_merged_mcp_config

            merged = get_merged_mcp_config(
                cwd, extra_servers=extra_mcp_servers, trust_cwd=trust_cwd
            )
            config = _normalize_mcp_config(merged)
        else:
            from mewbo_tools.integration.mcp import _load_mcp_config

            config = _normalize_mcp_config(
                _load_mcp_config(mcp_config_path, cwd=cwd, trust_cwd=trust_cwd)
            )
        pool = get_mcp_pool()
        # refresh_if_config_changed diffs against the pool's previous config
        # and disconnects servers that are no longer present — essential when
        # the same long-lived pool serves multiple project scopes in the API
        # process. connect_all is additive-only and would let a previous
        # project's MCP servers bleed into the current project's tool list.
        asyncio.run(pool.refresh_if_config_changed(config))
        details = pool.get_all_tool_details()
        # If pool connected but discovered zero tools across all servers,
        # treat that as a failure and fall through to the direct-discovery path.
        total_tools = sum(len(tools) for tools in details.values())
        if total_tools == 0:
            logging.debug("Pool connected but found no tools, falling back to direct")
            return None
        return details
    except Exception as exc:
        logging.debug("Pool-based MCP discovery failed, will discover directly: {}", exc)
        return None


def _ensure_auto_manifest(
    mcp_config_path: str,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
    trust_cwd: bool = True,
) -> str | None:
    manifest_path = _default_manifest_cache_path()
    existing_manifest: dict[str, JsonValue] | None = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except Exception as exc:
            logging.warning("Failed to read existing MCP manifest: {}", exc)

    # Non-blocking startup: when the cached manifest was
    # built from the SAME MCP config we have now, reuse it instead of doing a
    # blocking live connect. A slow/dead server in an unchanged config thus
    # never stalls the ready banner — its tools come from cache and the pool
    # connects lazily on first use (get_or_connect). A config edit changes the
    # hash and re-triggers discovery; `/mcp` refresh clears the cache to force
    # a re-probe (e.g. to pick up a server that has since come back up).
    config_for_hash = _resolve_mcp_config(
        mcp_config_path, cwd=cwd, extra_mcp_servers=extra_mcp_servers, trust_cwd=trust_cwd
    )
    current_hash = _config_fingerprint(config_for_hash)
    if (
        existing_manifest is not None
        and current_hash is not None
        and existing_manifest.get("config_hash") == current_hash
        and _manifest_has_mcp_tools(existing_manifest)
    ):
        logging.debug("MCP config unchanged; using cached manifest (non-blocking startup)")
        return manifest_path

    # Try pool-based discovery first (faster, persistent connections)
    pool_tools = _try_pool_discovery(
        mcp_config_path, cwd=cwd, extra_mcp_servers=extra_mcp_servers, trust_cwd=trust_cwd
    )

    mcp_module = _load_mcp_support()
    mcp_tools: dict[str, list[dict[str, object]]] = {}
    failures: dict[str, Exception] = {}
    global_failure: Exception | None = None

    if pool_tools is not None:
        mcp_tools = pool_tools
        logging.debug("MCP tools discovered via connection pool")
    elif mcp_module is None:
        global_failure = RuntimeError("MCP support is not installed.")
    else:
        try:
            if extra_mcp_servers:
                from mewbo_core.config import get_merged_mcp_config

                config = mcp_module._normalize_mcp_config(
                    get_merged_mcp_config(
                        cwd, extra_servers=extra_mcp_servers, trust_cwd=trust_cwd
                    )
                )
            else:
                config = mcp_module._load_mcp_config(
                    mcp_config_path if mcp_config_path else None,
                    cwd=cwd,
                    trust_cwd=trust_cwd,
                )
            mcp_tools, failures = mcp_module.discover_mcp_tool_details_with_failures(config)
        except Exception as exc:
            logging.warning("Failed to auto-discover MCP tools: {}", exc)
            global_failure = exc

    payload = _build_manifest_payload(mcp_tools)
    # Stamp the config fingerprint so the next startup can short-circuit the
    # blocking discovery when the config is unchanged (the cache-hit check above).
    if current_hash is not None:
        payload["config_hash"] = current_hash
    if (failures or global_failure) and existing_manifest:
        payload_tools = payload.get("tools", [])
        if not isinstance(payload_tools, list):
            payload_tools = []
        tools_by_id: dict[str, dict[str, JsonValue]] = {}
        for tool in payload_tools:
            if not isinstance(tool, dict):
                continue
            tool_id = tool.get("tool_id")
            if not tool_id:
                continue
            tools_by_id[str(tool_id)] = tool
        cached_tools = existing_manifest.get("tools", [])
        if not isinstance(cached_tools, list):
            cached_tools = []
        for tool in cached_tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("kind") != "mcp":
                continue
            server_name = tool.get("server")
            if not isinstance(server_name, str) or not server_name:
                continue
            if not global_failure and server_name not in failures:
                continue
            tool_id = tool.get("tool_id")
            if not tool_id:
                continue
            disabled_tool = dict(tool)
            disabled_tool["enabled"] = False
            if global_failure:
                disabled_tool["disabled_reason"] = f"Discovery failed: {global_failure}"
            else:
                disabled_tool["disabled_reason"] = f"Discovery failed: {failures[server_name]}"
            tools_by_id[tool_id] = disabled_tool
        payload["tools"] = list(tools_by_id.values())
    try:
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    except OSError as exc:
        logging.warning("Failed to write MCP tool manifest: {}", exc)
        return manifest_path if os.path.exists(manifest_path) else None
    return manifest_path


def load_registry(
    manifest_path: str | None = None,
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
    trust_cwd: bool = True,
) -> ToolRegistry:
    """Load tool registry, auto-discovering MCP tools when configured.

    *trust_cwd* is the caller's explicit statement about *cwd*: ``False`` means
    the directory holds content this deployment did not author, so neither its
    own ``.mcp.json`` nor any beneath it may name a server. The decision is
    carried onto every ``MCPToolRunner`` built here, because a runner
    re-resolves the config at invocation time — covering the boundary at build
    time alone would leak at call time.
    """
    from mewbo_core.config import is_untrusted_cwd

    # A directory registered as untrusted overrides an affirmative argument: the
    # caller that CREATED it knows more than the caller that passed it along.
    trust_cwd = trust_cwd and not is_untrusted_cwd(cwd)
    if manifest_path is None:
        mcp_config_path = get_mcp_config_path()
        # Check for CWD .mcp.json even when no global config exists
        has_cwd_mcp = False
        if cwd and trust_cwd:
            from pathlib import Path as _Path

            has_cwd_mcp = (_Path(cwd) / ".mcp.json").is_file()
        # Also check for subtree .mcp.json files
        has_subtree_mcp = False
        if cwd and trust_cwd and not has_cwd_mcp:
            from mewbo_core.config import _discover_subtree_mcp_json

            has_subtree_mcp = bool(_discover_subtree_mcp_json(cwd))
        if (
            (mcp_config_path and os.path.exists(mcp_config_path))
            or has_cwd_mcp
            or has_subtree_mcp
            or extra_mcp_servers
        ):
            manifest_path = _ensure_auto_manifest(
                mcp_config_path or "",
                cwd=cwd,
                extra_mcp_servers=extra_mcp_servers,
                trust_cwd=trust_cwd,
            )

    if not manifest_path:
        return _default_registry()

    manifest_path = os.path.abspath(manifest_path)
    if not os.path.exists(manifest_path):
        logging.warning("Tool manifest not found: {}", manifest_path)
        return _default_registry()

    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("Failed to load tool manifest: {}", exc)
        return _default_registry()

    registry = ToolRegistry()
    # Built once, up front, and reused for BOTH the declared-limit overlay below
    # and the merge of un-manifested built-ins further down — the same object
    # that used to be built only for the merge, so this costs no extra probe.
    builtin_registry = _default_registry()
    declared_by_id = {
        spec.tool_id: spec for spec in builtin_registry.list_specs(include_disabled=True)
    }
    for tool in manifest.get("tools", []):
        kind = tool.get("kind", "local")
        prompt_path = tool.get("prompt")
        if kind == "local":
            module_path = tool.get("module")
            class_name = tool.get("class")
            if not module_path or not class_name:
                logging.warning("Skipping tool with missing module/class: {}", tool)
                continue
            factory = _import_factory(module_path, class_name)
        else:
            mcp_module = _load_mcp_support()
            if mcp_module is None:
                logging.warning(
                    "Skipping MCP tool because MCP support is not installed: {}",
                    tool,
                )
                continue
            MCPToolRunner = mcp_module.MCPToolRunner

            server_name = tool.get("server")
            tool_name = tool.get("tool")
            if not server_name or not tool_name:
                logging.warning("Skipping MCP tool with missing server/tool: {}", tool)
                continue

            def _mcp_factory(
                server_name: str = server_name,
                tool_name: str = tool_name,
                _cwd: str | None = cwd,
                _trust_cwd: bool = trust_cwd,
            ) -> ToolRunner:
                return MCPToolRunner(
                    server_name=server_name,
                    tool_name=tool_name,
                    cwd=_cwd,
                    trust_cwd=_trust_cwd,
                )

            factory = _mcp_factory

        spec = ToolSpec(
            tool_id=tool.get("tool_id", ""),
            name=tool.get("name", tool.get("tool_id", "")),
            description=tool.get("description", ""),
            factory=factory,
            enabled=tool.get("enabled", True),
            kind=kind,
            prompt_path=prompt_path,
            read_only=bool(tool.get("read_only", False)),
            capability=tool.get("capability"),
            poll=bool(tool.get("poll", False)),
            poll_when_args=tuple(tool.get("poll_when_args") or ()),
            # A field this rebuild forgets is not defaulted deliberately — it is
            # LOST, and it takes the reason someone declared it with it. These
            # four were dropped: every deployment that reaches the registry
            # through a manifest (any deployment with an MCP config, which is
            # the normal one) silently ran all 159 tools at the 2000-char
            # default, so the caps declared on shell and on the file reader were
            # dead code exactly where they mattered. The symptom was a model
            # given the middle of a file removed, arguing with a tool that had
            # told it there was no such limit.
            max_result_chars=int(
                tool.get("max_result_chars", ToolSpec.max_result_chars)
            ),
            timeout=float(tool.get("timeout", ToolSpec.timeout)),
            concurrency_safe=bool(
                tool.get("concurrency_safe", ToolSpec.concurrency_safe)
            ),
            interrupt_behavior=str(
                tool.get("interrupt_behavior", ToolSpec.interrupt_behavior)
            ),
            metadata={
                key: value
                for key, value in tool.items()
                if key
                not in {
                    "tool_id",
                    "name",
                    "description",
                    "module",
                    "class",
                    "enabled",
                    "kind",
                    "prompt",
                    "read_only",
                    "capability",
                    "poll",
                    "poll_when_args",
                    "max_result_chars",
                    "timeout",
                    "concurrency_safe",
                    "interrupt_behavior",
                }
            },
        )
        if not spec.tool_id:
            logging.warning("Skipping tool with empty tool_id: {}", tool)
            continue

        # THE PARITY GUARD, and the repair, in one comparison. A manifest is a
        # CACHE of the built-in declarations and can outlive them: a file written
        # before a field existed carries no key for it, so the reader above
        # defaults it and the declaration dies exactly where it is needed. That
        # is not hypothetical — a live deployment ran all 159 tools at the
        # 2000-char default while `read_file` declared 200_000, because its
        # cached manifest predated the field and the config hash still matched,
        # so no regeneration was ever triggered.
        #
        # Overlaying at LOAD rather than bumping a cache-invalidation hash is
        # deliberate: an already-written manifest must self-heal with no operator
        # step, and a hash bump repairs nothing until something happens to
        # rewrite the file. Loud, because a silent repair leaves the stale file
        # in place and the next reader re-derives the same surprise.
        declared = declared_by_id.get(spec.tool_id)
        if declared is not None:
            mismatches = spec.knob_mismatches(declared)
            if mismatches:
                logging.warning(
                    "Tool manifest is stale for {}: {} — using the built-in "
                    "declaration. Regenerate the manifest to silence this.",
                    spec.tool_id,
                    ", ".join(
                        f"{name} manifest={mine!r} declared={theirs!r}"
                        for name, (mine, theirs) in sorted(mismatches.items())
                    ),
                )
                spec = spec.with_declared_knobs(declared)
        registry.register(spec)

    if not registry.list_specs(include_disabled=True):
        return builtin_registry

    existing_ids = {spec.tool_id for spec in registry.list_specs(include_disabled=True)}
    for spec in builtin_registry.list_specs(include_disabled=True):
        if spec.tool_id == TOOL_SEARCH_TOOL_ID:
            # Skip — re-registered below so its factory binds to ``registry``,
            # not the throwaway ``builtin_registry`` instance.
            continue
        if spec.tool_id in existing_ids:
            continue
        registry.register(spec)
        existing_ids.add(spec.tool_id)

    # Bind tool_search to the final, merged registry so it can search all specs.
    _register_tool_search(registry)

    set_available_tools([spec.tool_id for spec in registry.list_specs()])
    return registry


class ToolRegistryCache:
    """Process-wide reuse of built ``ToolRegistry`` objects, keyed by build inputs.

    ``Orchestrator.__init__`` builds the tool registry on every run (per-query),
    paying ``load_registry``'s cost — reading the MCP manifest, constructing every
    ``ToolSpec``, probing Home-Assistant/LSP status — *before* the run emits its
    first event. That work is identical across runs whose inputs are identical, so
    the result is cached and the SAME registry handed back on the next run within a
    session (kill the blank-shell wait).

    The key is exactly the set of inputs that change what ``load_registry``
    produces: the project ``cwd``, any plugin-contributed ``extra_mcp_servers``,
    and the resolved MCP-config fingerprint (so an edit to ``mcp.json`` still forces
    a rebuild — the same signal that gates the manifest rebuild in
    ``_ensure_auto_manifest``). Allowed-tools scoping is applied per-run downstream
    (``filter_specs`` over ``list_specs``), never at build time, so it is
    deliberately NOT part of the key.
    """

    def __init__(self) -> None:
        """Initialize an empty, lock-guarded cache."""
        self._lock = threading.Lock()
        self._cache: dict[str, ToolRegistry] = {}

    @staticmethod
    def _key(
        cwd: str | None,
        extra_mcp_servers: dict[str, dict] | None,
        trust_cwd: bool = True,
    ) -> str:
        servers = (
            json.dumps(extra_mcp_servers, sort_keys=True, default=str)
            if extra_mcp_servers
            else ""
        )
        config = _resolve_mcp_config(
            get_mcp_config_path() or "",
            cwd=cwd,
            extra_mcp_servers=extra_mcp_servers,
            trust_cwd=trust_cwd,
        )
        fingerprint = _config_fingerprint(config) or ""
        # `trust_cwd` is part of the key, not just the build: two scopes sharing
        # a cwd but disagreeing about its trust must never share a registry —
        # the trusted one's cached MCP runners carry `trust_cwd=True`.
        trust = "trusted" if trust_cwd else "untrusted"
        return "\x00".join((cwd or "", servers, fingerprint, trust))

    def get_or_build(
        self,
        *,
        cwd: str | None = None,
        extra_mcp_servers: dict[str, dict] | None = None,
        trust_cwd: bool = True,
    ) -> ToolRegistry:
        """Return a cached registry for these inputs, building exactly once."""
        from mewbo_core.config import is_untrusted_cwd

        trust_cwd = trust_cwd and not is_untrusted_cwd(cwd)
        key = self._key(cwd, extra_mcp_servers, trust_cwd)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        # Build OUTSIDE the lock so a slow ``load_registry`` for one scope never
        # blocks another scope's lookup. A rare concurrent cold miss builds twice;
        # ``setdefault`` makes both callers converge on the first-stored instance.
        registry = load_registry(
            cwd=cwd, extra_mcp_servers=extra_mcp_servers, trust_cwd=trust_cwd
        )
        with self._lock:
            return self._cache.setdefault(key, registry)

    def clear(self) -> None:
        """Drop every cached registry."""
        with self._lock:
            self._cache.clear()


_REGISTRY_CACHE = ToolRegistryCache()


def get_or_build_registry(
    *,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
    trust_cwd: bool = True,
) -> ToolRegistry:
    """Return a cached ``ToolRegistry`` for these inputs, building once per scope.

    The single seam ``Orchestrator`` uses to avoid rebuilding the registry on
    every run in a session. Falls through to :func:`load_registry` on a cache
    miss. See :class:`ToolRegistryCache` for the keying contract.

    Pass ``trust_cwd=False`` when *cwd* holds content this deployment did not
    author. A caller that does not know can leave it alone and register the
    directory with :data:`mewbo_core.config.register_untrusted_cwd` instead —
    that is read here too, and overrides an affirmative argument.
    """
    return _REGISTRY_CACHE.get_or_build(
        cwd=cwd, extra_mcp_servers=extra_mcp_servers, trust_cwd=trust_cwd
    )


def reset_registry_cache() -> None:
    """Drop all cached registries (tests; an explicit ``/mcp`` refresh)."""
    _REGISTRY_CACHE.clear()


def filter_specs(
    specs: list[ToolSpec],
    *,
    allowed: list[str] | None = None,
    denied: list[str] | None = None,
    capability_mode: str = "all",
) -> list[ToolSpec]:
    """Filter tool specs by allowlist, capability mode, and/or denylist.

    *allowed* is THREE-STATE and tested with ``is None``, never truthiness:
    ``None`` is unrestricted (no allowlist gate), ``[]`` grants NOTHING, and a
    non-empty list grants exactly those ids. Collapsing ``[]`` into ``None``
    would turn "this principal gets no tools" into "this principal gets every
    tool" — the fail-open direction, on the gate that binds an agent's whole
    tool surface.

    When the gate applies, only specs whose ``tool_id`` is in the list are
    kept — EXCEPT ``always_load`` specs (the ``tool_search`` tool),
    which are exempt from the allowlist gate so a scoped sub-agent never
    loses the means to fetch its deferred MCP tools.

    *capability_mode* is a coarse privilege pre-filter
    applied AFTER the allowlist and layered UNDER it: it can only remove more
    tools, never resurrect one the allowlist dropped. Only specs whose
    :meth:`ToolSpec.capability_tier` is admitted by the mode survive — see
    ``CapabilityMode`` for the tier law. ``always_load`` is exempt here too
    (harmless discovery), mirroring the allowlist gate; ``"all"`` (the default)
    and any unrecognised mode skip the gate entirely.

    Then any spec whose ``tool_id`` appears in *denied* (merged with the config
    ``agent.default_denied_tools``) is removed.  Deny always takes precedence
    over allow AND capability_mode — an explicit deny removes even an
    ``always_load`` tool.
    """
    if allowed is not None:
        allowed_set = set(allowed)
        specs = [s for s in specs if s.tool_id in allowed_set or is_always_load(s)]

    if _CAPABILITY_MODE_TIERS.get(capability_mode) is not None:
        specs = [
            s
            for s in specs
            if is_always_load(s)
            or capability_mode_admits(capability_mode, s.capability_tier())
        ]

    denied_set: set[str] = set(denied or [])
    config_denied_raw = get_config_value("agent", "default_denied_tools", default=[])
    if isinstance(config_denied_raw, str):
        config_denied_raw = [s.strip() for s in config_denied_raw.split(",") if s.strip()]
    denied_set |= set(config_denied_raw or [])

    if denied_set:
        specs = [s for s in specs if s.tool_id not in denied_set]

    return specs


__all__ = [
    "TOOL_SEARCH_TOOL_ID",
    "CapabilityMode",
    "ToolRegistry",
    "ToolRegistryCache",
    "ToolSpec",
    "capability_mode_admits",
    "classify_tool_scope",
    "filter_specs",
    "get_or_build_registry",
    "is_always_load",
    "is_deferred",
    "load_registry",
    "reset_registry_cache",
]
