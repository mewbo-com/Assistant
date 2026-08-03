#!/usr/bin/env python3
"""Workspace containment — the per-agent filesystem firebreak.

``workspace_mode`` is the SECOND privilege axis a spawn can attenuate, orthogonal
to ``capability_mode``: where ``capability_mode`` gates *which tools* an
agent holds, ``workspace_mode`` gates *which paths* those tools may touch. The
three tiers (widest → narrowest):

* ``full_access``  — no path restriction. Root default.
* ``workspace_write`` — reads and writes confined to the agent's workspace root
  (plus the Mewbo-owned scratch roots).
* ``read_only`` — reads confined to the workspace root; NO writes anywhere.

``WorkspaceContainment`` is an atomic frozen dataclass carrying the resolved
policy (``mode`` + ``root``). It lives in ``mewbo_core`` — NOT ``mewbo_tools`` —
because BOTH sides of the DAG need it: the loop (``mewbo_core.loop.tool_use_loop``)
builds it and forces the authoritative root, and the path guard
(``mewbo_tools.core.resolve_safe_path``) enforces it. Dependencies flow
``core → tools`` (tools imports core, never the reverse), so the shared type sits
at the bottom in core and both layers import DOWN to it.

**Where the firebreak is actually drawn.** The ROOT agent runs at ``full_access``
— it acts directly for the operator, so confining it would restrict a user rather
than attenuate a privilege. Attenuation happens ACROSS A SPAWN: ``SpawnAgentTask``
defaults a child to ``workspace_write``, and min-wins narrowing means no
descendant can widen back out. So the tier that matters is the one a child
inherits, not the one the root starts at.

**The switch.** ``agent.workspace_enforcement`` (default ``True``) gates whether a
containment is built at all. With it off, no ``WorkspaceContainment`` is ever
activated and every path resolves through the tenant union regardless of tier —
the tiers are still carried and narrowed, but they govern nothing. Keep that in
view when reading a tier off an ``AgentContext``: the value alone does not tell
you whether it is enforced.

**Loop → tool bridge.** The path guard's callers live in ``mewbo_tools`` and
receive their arguments as JSON (which cannot carry a live object), and the loop
(in ``mewbo_core``) must not import an app or reach sideways into ``mewbo_tools``.
The active containment therefore travels through a context variable set by the
loop around each tool's execution and read by ``resolve_safe_path`` as a fallback
when no explicit ``containment=`` is passed. ``contextvars`` propagate into
``asyncio.to_thread`` (the sync-tool execution path), so a threaded ``tool.run``
sees the same active containment as the awaiting frame that set it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT

# The three workspace-containment tiers. A plain module type alias (not owned by
# ``AgentContext``) so both the spawn schema and the config validator can pin the
# same vocabulary via ``typing.get_args`` — the same idiom ``CapabilityMode`` uses
# for ``capability_mode``.
WorkspaceMode = Literal["read_only", "workspace_write", "full_access"]

# Monotonic privilege ranking for ``workspace_mode`` narrowing: lower rank
# = more restrictive. The narrowing itself is the shared ``AgentContext._narrow``
# min-wins helper; this table only encodes the ordering, mirroring
# ``AgentContext._CAPABILITY_MODE_RANK`` for the other axis.
WORKSPACE_MODE_RANK: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "full_access": 2,
}

# Mewbo-owned scratch root — mirrors the constant in
# ``mewbo_tools.core._get_allowed_roots``. Scoping to ``/tmp/mewbo`` (not all of
# ``/tmp``) is the tenant boundary the tools guard already draws; a contained
# agent keeps the same carve-out so plan-mode + widget scratch writes still land.
_SCRATCH_ROOT = "/tmp/mewbo"


@dataclass(frozen=True, slots=True)
class WorkspaceContainment:
    """Resolved filesystem-containment policy for one agent.

    ``mode`` is one of :data:`WorkspaceMode`; ``root`` is the agent's workspace
    directory (its loop ``cwd``). Behaviour intrinsic to the data lives ON the
    model — ``permits_read``/``permits_write`` own the tier semantics so the path
    guard delegates rather than re-deriving them with a service-side ``if mode ==``
    switch. Paths arrive as method ARGS (the model never reaches for the
    filesystem itself), keeping it trivially testable.
    """

    mode: str
    root: str

    @property
    def active(self) -> bool:
        """True when this containment restricts anything (``mode != full_access``).

        ``full_access`` is the no-op tier — an ``active`` containment is the
        only kind the loop ever builds or the guard ever enforces.
        """
        return self.mode != "full_access"

    def allowed_roots(self) -> tuple[str, ...]:
        """The directories this agent may touch: workspace root + scratch roots.

        The workspace ``root`` plus the two Mewbo-owned scratch roots
        (``/tmp/mewbo`` and the plan-scratch ``PLAN_DIR_ROOT``, which is itself
        under ``/tmp/mewbo`` but named explicitly for clarity + future-proofing).
        An empty ``root`` contributes nothing, so a containment with no root
        confines to the scratch roots alone.
        """
        roots: list[str] = []
        if self.root:
            roots.append(self.root)
        roots.append(_SCRATCH_ROOT)
        if PLAN_DIR_ROOT not in roots:
            roots.append(PLAN_DIR_ROOT)
        return tuple(roots)

    def _under_allowed(self, path: str) -> bool:
        """True when *path* physically resolves under an allowed root.

        Realpath-prefix semantics matching the ``FileCatalog`` jail precedent:
        both the candidate and each allowed root are ``realpath``-normalised
        (following symlinks) and compared with ``startswith(root + os.sep)``. This
        is DELIBERATELY stricter than the tenant-union's dual logical/physical
        view — a symlink whose *logical* path sits inside the workspace but whose
        *target* escapes it is rejected, closing the ``/tmp`` symlink → ``/etc/
        passwd`` hole the broad union tolerates. Containment is a firebreak, so
        the physical target is authoritative.
        """
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        for root in self.allowed_roots():
            try:
                real_root = os.path.realpath(root)
            except (OSError, ValueError):
                continue
            if real == real_root or real.startswith(real_root + os.sep):
                return True
        return False

    def permits_read(self, path: str) -> bool:
        """True when this agent may READ *path*.

        ``full_access`` reads anything; every narrower tier confines reads to the
        allowed roots.
        """
        if self.mode == "full_access":
            return True
        return self._under_allowed(path)

    def permits_write(self, path: str) -> bool:
        """True when this agent may WRITE *path*.

        ``read_only`` writes NOTHING (anywhere); ``full_access`` writes anything;
        ``workspace_write`` confines writes to the allowed roots.
        """
        if self.mode == "read_only":
            return False
        if self.mode == "full_access":
            return True
        return self._under_allowed(path)


# The active containment for the current execution context. The loop sets it
# around a tool's run when its own containment is active; ``resolve_safe_path``
# reads it as the fallback when no explicit ``containment=`` is passed. Defaults
# to ``None`` — no active containment, so the plain tenant union applies.
_ACTIVE_CONTAINMENT: ContextVar[WorkspaceContainment | None] = ContextVar(
    "mewbo_active_containment", default=None
)


def get_active_containment() -> WorkspaceContainment | None:
    """Return the containment active for the current execution context, or ``None``.

    Read by ``mewbo_tools.core.resolve_safe_path`` (fallback) and the LSP tool so
    they enforce the same firebreak the loop established, without the loop having
    to thread a live object through JSON tool arguments.
    """
    return _ACTIVE_CONTAINMENT.get()


@contextmanager
def active_containment(containment: WorkspaceContainment | None) -> Iterator[None]:
    """Set the active containment for the duration of the ``with`` block.

    A ``None`` (or inactive) *containment* is a no-op that leaves the current
    active containment untouched, so a full_access / enforcement-off tool run
    pays nothing. Reset via the ``ContextVar`` token in
    ``finally`` so nesting and early exits never leak.
    """
    if containment is None or not containment.active:
        yield
        return
    token = _ACTIVE_CONTAINMENT.set(containment)
    try:
        yield
    finally:
        _ACTIVE_CONTAINMENT.reset(token)


# The directory the current execution context is working in — a resolved project,
# or the session's own scratch directory when no project is bound. Set by the loop
# around EVERY tool run, unlike the containment above: the shell sandbox scopes
# DATA (which project's files are reachable), which is a different axis from
# ``workspace_mode``, which scopes PRIVILEGE. Conflating them is why the root
# agent — always ``full_access``, so never contained — had an unscoped shell.
#
# It exists as its own carrier because the shell's scope must not be derivable
# from tool ARGUMENTS: with no containment the loop honours a model-supplied
# ``root``, and a model-chosen root would be a model-chosen sandbox.
_ACTIVE_PROJECT_ROOT: ContextVar[str | None] = ContextVar(
    "mewbo_active_project_root", default=None
)


def get_active_project_root() -> str | None:
    """Return the directory the current execution context is working in.

    Read by the shell sandbox to decide which configured project is the session's
    own, and therefore which of the others to deny.
    """
    return _ACTIVE_PROJECT_ROOT.get()


@contextmanager
def active_project_root(root: str | None) -> Iterator[None]:
    """Publish *root* as the working directory for the duration of the block.

    A falsy *root* is a no-op, so a loop with no workspace pays nothing. Reset via
    the token in ``finally`` so nesting and early exits never leak.
    """
    if not root:
        yield
        return
    token = _ACTIVE_PROJECT_ROOT.set(root)
    try:
        yield
    finally:
        _ACTIVE_PROJECT_ROOT.reset(token)


# The confining-scope factory for a subprocess spawn: given the active root a
# caller is working in, returns a context manager yielding a ``preexec_fn`` hook
# (or ``None``). Signature mirrors
# ``mewbo_tools.integration.landlock.shell_preexec_for_root``.
ShellPreexecFactory = Callable[[str | None], AbstractContextManager[Callable[[], None] | None]]

# Down-only push seam (mirrors ``capabilities.register_session_capability_provider``
# / ``plugins.register_builtin_root``): ``mewbo_tools`` owns the Landlock deny-list
# that confines a spawned subprocess (``ShellScope``, one layer above core in the
# DAG), and `mewbo_graph` is a SEPARATE package one layer above core that also
# needs it (its git executor + the scip-python resolver both spawn subprocesses).
# Neither core nor `mewbo_graph` may import `mewbo_tools` — root CLAUDE.md refuses
# a 13th reach-up out of core, and `mewbo_graph`'s own CLAUDE.md is exactly as
# absolute ("imports down into core + pydantic and NOTHING ELSE — never
# mewbo_tools"). So `mewbo_tools.integration.landlock` registers its factory HERE
# at its own import time (an ordinary down-import of core, needing no guard on
# that side), and this ONE seam serves every consumer above it in the DAG that
# cannot reach the concrete implementation directly. Unregistered — a core-only
# install that never imports `mewbo_tools` — every caller of
# :func:`shell_preexec_scope` gets an unscoped spawn.
_shell_preexec_factory: ShellPreexecFactory | None = None


def register_shell_preexec_factory(factory: ShellPreexecFactory) -> None:
    """Register the confining scope factory subprocess spawns compose through.

    Last registration wins — there is exactly one real implementation
    (``mewbo_tools``'s Landlock-backed one); a test may register a fake here to
    assert confinement without depending on that package.
    """
    global _shell_preexec_factory
    _shell_preexec_factory = factory


def reset_shell_preexec_factory() -> None:
    """Drop the registered factory (test isolation seam)."""
    global _shell_preexec_factory
    _shell_preexec_factory = None


def shell_preexec_scope(
    active_root: str | None,
) -> AbstractContextManager[Callable[[], None] | None]:
    """Build the confining spawn scope for *active_root*.

    Compose as ``with shell_preexec_scope(root) as hook: Popen(...,
    preexec_fn=hook)``. Every core/``mewbo_graph`` spawn site that wants
    Landlock confinement calls this instead of reaching for ``mewbo_tools``
    directly. Degrades to ``nullcontext(None)`` — an unscoped spawn — whenever
    nothing is registered.
    """
    if _shell_preexec_factory is None:
        return nullcontext(None)
    return _shell_preexec_factory(active_root)


__all__ = [
    "WORKSPACE_MODE_RANK",
    "ShellPreexecFactory",
    "WorkspaceContainment",
    "WorkspaceMode",
    "active_containment",
    "active_project_root",
    "get_active_containment",
    "get_active_project_root",
    "register_shell_preexec_factory",
    "reset_shell_preexec_factory",
    "shell_preexec_scope",
]
