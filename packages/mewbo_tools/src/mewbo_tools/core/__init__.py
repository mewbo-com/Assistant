#!/usr/bin/env python3
"""Core tool implementations and shared utilities."""

from __future__ import annotations

import os
from pathlib import Path

from mewbo_core.config import get_config_value
from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT
from mewbo_core.workspaces.workspace import (
    WorkspaceContainment,
    get_active_containment,
    get_active_project_root,
)

from mewbo_tools.integration.landlock import ShellScope


def _path_scoping_enabled() -> bool:
    """Whether ``agent.path_scope_to_active_project`` governs this process.

    Read as its OWN switch, deliberately not ``agent.shell_sandbox``: that one
    also decides whether a kernel mechanism applies at all, so a deployment on
    a kernel without Landlock turns it off — and must not thereby lose the
    argument check as well.

    Cost: O(1).
    """
    return bool(get_config_value("agent", "path_scope_to_active_project", default=False))


def _active_scope_roots() -> tuple[str, ...]:
    """The session's own project roots when path scoping applies, else ``()``.

    ``()`` means "not scoped" and every caller reads it as *historical
    behaviour*, never as "nothing is allowed". It arises two ways: the flag is
    off, or no active project root was published for this execution context —
    a direct library caller or a test, since ``ToolUseLoop`` publishes one
    around every tool call.

    The resolution itself is :meth:`ShellScope.readmitted_for`, i.e. the same
    one the kernel-level shell sandbox uses. The two guards share the answer to
    "which project is this session's" and keep their own policy switch, because
    only one of them depends on a kernel feature being present.

    Cost: O(configured projects).
    """
    if not _path_scoping_enabled():
        return ()
    return ShellScope.readmitted_for(get_active_project_root())


def _get_allowed_roots() -> list[Path]:
    """Return the roots a path argument may resolve under.

    Two shapes, chosen by :func:`_active_scope_roots`:

    * **Scoped** — the session's own project plus whatever that project
      re-admits through ``allowed_paths``. Neither another configured project
      nor the API host's own working directory (which is the harness itself on
      a container deployment) is reachable, matching what the shell sandbox
      already denies at the kernel.
    * **Historical union** — CWD ∪ every configured project path, the tenant
      boundary as it stood before scoping, byte-identical.

    Both shapes append the Mewbo-owned scratch roots: ``/tmp/mewbo`` (covering
    ``/tmp/mewbo/widgets`` and any future ephemeral subdir) and
    :data:`mewbo_core.tooling.exit_plan_mode.PLAN_DIR_ROOT`, so the edit and
    file tools can read/write the per-session plan scratch file under every
    scope. Scoping to ``/tmp/mewbo`` rather than all of ``/tmp`` keeps tools out
    of unrelated tempfiles, and per-session isolation INSIDE the plan root is
    enforced by ``is_inside_plan_dir()`` at the upper (permission) guard in
    ``tool_use_loop._plan_mode_permission`` — so listing it here relaxes
    nothing.

    Cost: O(configured projects).
    """
    scoped = _active_scope_roots()
    roots: list[Path] = [Path(p) for p in scoped]
    if not roots:
        roots.append(Path(os.getcwd()).resolve())
        projects: dict = get_config_value("projects", default={})
        for cfg in projects.values():
            raw = cfg.get("path", "") if isinstance(cfg, dict) else getattr(cfg, "path", "")
            if raw:
                p = Path(raw).expanduser().resolve()
                if p not in roots:
                    roots.append(p)
    for scratch in (Path(PLAN_DIR_ROOT).resolve(), Path("/tmp/mewbo").resolve()):
        if scratch not in roots:
            roots.append(scratch)
    return roots


def _under(child: str, parent: str) -> bool:
    """Whether *child* is *parent* or sits beneath it, on realpath components.

    A string prefix alone would make ``/srv/appdata`` land under ``/srv/app``.

    Cost: O(1).
    """
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _refuse_denied(path: str, resolved: Path) -> None:
    """Refuse *resolved* when it lands under a denied root. The ONE such check.

    A POST-condition at :func:`resolve_safe_path`'s return rather than a step
    inside root construction, because that function has two mutually exclusive
    exits — the containment branch and the tenant union — each building its own
    root set. Subtracting inside :func:`_get_allowed_roots` would be invisible
    to every contained sub-agent.

    The denial set is not re-derived here: it is
    :meth:`~mewbo_tools.integration.landlock.ShellScope.denied_for`, the same
    one the kernel-level shell sandbox compiles into its ruleset. That is the
    point — an operator who denies a directory got it denied to the shell and
    ALLOWED to ``read_file``, because this half never read
    ``agent.shell_denied_paths`` at all.

    Gated on ``agent.path_scope_to_active_project``, NOT on
    ``agent.shell_sandbox``: the two keep separate switches because
    ``shell_sandbox`` also decides whether a kernel mechanism applies, while an
    operator's ``shell_denied_paths`` declaration is policy, which this half can
    honour with no kernel at all.

    A re-admitted (``allowed``) root wins over a denial, the same precedence
    Landlock's compiled grants give it — that is what lets a project's
    ``allowed_paths`` reach a sibling checkout which is itself a configured
    project.

    **The one deliberate divergence from the shell's set:** with no active root
    published, the "every OTHER configured project" component is dropped, so a
    sibling configured project stays reachable here while Landlock denies it.
    Falling fully closed there would break every direct library caller, test and
    CLI invocation, none of which publish a root. The operator-declared and
    harness components still apply, which is what closes the harness source, the
    directory holding ``app.json``, and every ``shell_denied_paths`` entry.

    Cost: O(configured projects + denied paths).
    """
    if not _path_scoping_enabled():
        return
    active = get_active_project_root()
    denied, allowed = ShellScope.denied_for(active, include_other_projects=bool(active))
    if not denied:
        return
    target = os.path.realpath(resolved)
    if any(_under(target, a) for a in allowed):
        return
    hit = next((d for d in denied if _under(target, d)), None)
    if hit is None:
        return
    # Name what IS allowed, keeping the one-turn-restage error contract: a mute
    # denial trains the model to go hunting for a shell workaround instead.
    reachable = sorted(
        {str(r) for r in _get_allowed_roots() if not any(_under(str(r), d) for d in denied)}
        | set(allowed)
    )
    raise ValueError(
        f"Path '{path}' resolves under a denied root ({hit}), which this "
        f"deployment withholds from the agent. "
        f"Allowed roots: {', '.join(reachable)}."
    )


def resolve_safe_path(
    path: str,
    root: str | None = None,
    *,
    containment: WorkspaceContainment | None = None,
    write: bool = False,
) -> Path:
    """Resolve *path* against the allowed roots, then refuse a denied one.

    The two steps are deliberately separate: :func:`_resolve_unchecked` has two
    mutually exclusive exits that each build their own root set, so the denial
    is applied ONCE here, at the return both of them pass through.

    Cost: O(configured projects).
    """
    resolved = _resolve_unchecked(path, root, containment=containment, write=write)
    _refuse_denied(path, resolved)
    return resolved


def _resolve_unchecked(
    path: str,
    root: str | None = None,
    *,
    containment: WorkspaceContainment | None = None,
    write: bool = False,
) -> Path:
    """Resolve *path* and verify it falls under an allowed project root.

    Checks (in order): *root* if given, then the roots
    :func:`_get_allowed_roots` returns — the session's own project while
    ``agent.path_scope_to_active_project`` applies, otherwise every
    ``projects[*].path`` from the app config plus the process CWD.

    Two views of the path are checked so legitimate symlinks inside a
    project root are honored:

    * ``resolved`` — ``Path.resolve()``; normalizes ``..`` *and* follows
      symlinks. Authoritative physical location on disk.
    * ``logical``  — ``os.path.abspath``; normalizes ``..`` but preserves
      symlinks. Reflects the user's intent when a symlink lives inside
      an allowed root (e.g. ``<project>/shared`` → ``/mnt/external``).

    The path is accepted if *either* view lands under an allowed root.
    ``../`` escape attempts still fail both checks and are rejected.

    **Workspace containment.** When an ACTIVE
    :class:`~mewbo_core.workspaces.workspace.WorkspaceContainment` applies — passed
    explicitly, or (fallback) the one the loop set for the current execution
    context via :func:`~mewbo_core.workspaces.workspace.get_active_containment` — the broad
    tenant union above is COLLAPSED to the containment's own allowed roots
    (workspace root + Mewbo scratch), and a ``write=True`` call is additionally
    checked against the tier's write rule (``read_only`` permits none). Denials
    still name what IS allowed. ``containment=None`` with no active context
    containment ⇒ the tenant-union behaviour above. Which of the two applies is
    decided at the loop, which builds a containment only for an agent whose tier
    is narrower than ``full_access`` and only while ``agent.workspace_enforcement``
    is on.

    **Active-project scoping.** Independently of containment, the union above
    collapses to the session's own project (plus its ``allowed_paths`` and the
    scratch roots) whenever ``agent.path_scope_to_active_project`` is on and the
    loop published an active project root. This is the axis a root agent needs:
    it is always ``full_access``, so containment never applies to the session an
    operator actually drives, and the union would otherwise let it read every
    other configured project. In that mode an out-of-scope *root* argument is
    ignored rather than honoured, for the same reason the sandbox never derives
    its scope from tool arguments.

    Raises ``ValueError`` when the path is outside all roots.
    """
    candidate = Path(path)
    root_path = Path(root).resolve() if root else None

    if not candidate.is_absolute():
        base = root_path or Path(os.getcwd()).resolve()
        candidate = base / candidate
    resolved = candidate.resolve()
    logical = Path(os.path.abspath(candidate))

    # Containment firebreak: an explicit arg wins; else fall back to the
    # loop-established active containment for this execution context. Only an
    # ACTIVE (non-full_access) containment diverts to the strict path — an
    # inactive/absent one leaves the tenant-union behaviour below untouched.
    effective = containment if containment is not None else get_active_containment()
    if effective is not None and effective.active:
        return _resolve_within_containment(path, resolved, effective, write=write)

    # Build a single check list: explicit root first, then config roots.
    roots = _get_allowed_roots()
    if root_path is not None and root_path not in roots:
        # ``root`` is a tool ARGUMENT, and with no containment the loop passes a
        # model-supplied one straight through. Honouring one that lands outside
        # the session's own project would therefore make the scope model-chosen,
        # which is the trap ``ShellScope`` names for the shell — so under scoping
        # a root may narrow (a subdirectory) but never widen. Unscoped, it is
        # accepted as before.
        #
        # The gate is the FLAG, not ``_active_scope_roots()``. Reading an empty
        # tuple as "not scoped" inverted the no-published-root case: Landlock
        # with ``active=None`` denies every configured project, while this half
        # fell open AND re-admitted whatever ``root`` the model named — so
        # ``read_file(path="id_rsa", root="/home/…/.ssh")`` resolved on any run
        # that never published a root, which every ``/v1/structured`` run is.
        if not _path_scoping_enabled() or any(root_path.is_relative_to(r) for r in roots):
            roots.insert(0, root_path)
    # Also accept roots under their logical (symlink-preserving) form so a
    # project whose configured path traverses a symlink still matches the
    # user's logical view of the path.
    logical_roots: list[Path] = []
    for r in roots:
        lr = Path(os.path.abspath(r))
        if lr != r and lr not in roots and lr not in logical_roots:
            logical_roots.append(lr)

    for r in roots:
        try:
            resolved.relative_to(r)
            return resolved
        except ValueError:
            pass
    for r in [*roots, *logical_roots]:
        try:
            logical.relative_to(r)
            return logical
        except ValueError:
            continue

    # Name what IS allowed: a denial the model can act on converges in one
    # turn; a mute one sends it hunting for shell workarounds instead.
    allowed = ", ".join(sorted(str(r) for r in roots))
    raise ValueError(
        f"Path '{path}' resolves outside all allowed project roots. "
        f"Allowed roots: {allowed}."
    )


def _resolve_within_containment(
    path: str,
    resolved: Path,
    containment: WorkspaceContainment,
    *,
    write: bool,
) -> Path:
    """Enforce an active :class:`WorkspaceContainment` on an already-resolved path.

    The union of tenant roots is collapsed to ``containment.allowed_roots()`` and
    the tier semantics are delegated to the containment model (behaviour on the
    data): a ``write=True`` call must satisfy ``permits_write`` (``read_only``
    permits none anywhere), a read ``permits_read``. Only the PHYSICAL ``resolved``
    view is honoured — the tenant-union's logical/symlink-preserving second view is
    deliberately NOT extended here, because accepting a symlink whose target
    escapes the workspace is exactly the firebreak breach containment exists to
    prevent (realpath-prefix semantics; see ``WorkspaceContainment._under_allowed``).
    Denials name what IS allowed, keeping the one-turn-restage error contract.
    """
    target = str(resolved)
    permitted = (
        containment.permits_write(target) if write else containment.permits_read(target)
    )
    if permitted:
        return resolved
    allowed = ", ".join(sorted(containment.allowed_roots()))
    verb = "write" if write else "read"
    raise ValueError(
        f"Path '{path}' is outside this agent's workspace (mode "
        f"'{containment.mode}', operation '{verb}'). Allowed roots: {allowed}."
    )


__all__ = ["resolve_safe_path"]
