"""Kernel-enforced filesystem scoping for the shell tool's subprocesses.

Every other tool takes a *path argument* that ``resolve_safe_path`` validates
before anything runs. The shell takes an opaque command string, so there is
nothing to validate: confining its starting ``cwd`` selects where the process
begins and says nothing about what it may read. ``cat``, ``grep``, ``python3``
and ``awk`` each walk straight out of a confined ``cwd`` — measured, not
assumed.

The control therefore binds below the tool layer, at the process-spawn seam,
where the kernel denies the syscall regardless of which binary the model chose.

**This is a DENY-list, and that direction is the whole design.** The obvious
shape — enumerate what the agent may reach — was built first and failed twice
for the same reason: the enumeration has to include the entire runtime, and
anything missed breaks the harness rather than confining it. Both misses were
silent (a virtualenv's ``site-packages`` surfaces as ``ModuleNotFoundError``, not
as a denial, because an unreadable directory just makes Python's path finder come
up empty). Naming what must be *denied* is a short, knowable list; naming what
must be *allowed* is every dependency of every command an agent might run.

Landlock has no deny rule — it grants beneath a path and refuses everything
unnamed — so a denial is *compiled* into grants: walk the ancestry of each denied
path and, at every level, grant the siblings that do not lead to a denial. That
is mechanical (a ``listdir`` at build time), not a list anyone maintains, which
is what makes it safe in a way the allowlist was not. Measured on a real
deployment shape: ~95 rules, about a millisecond to compile and apply.

A true deny primitive would be a mount namespace with an empty overmount, and it
is **not available here**: unprivileged user namespaces are refused on both the
host (``apparmor_restrict_unprivileged_userns=1``) and inside the container
(seccomp). Buying one would mean granting ``CAP_SYS_ADMIN`` — a larger hole than
this closes.

Two properties make it safe to derive the scope per spawn:

* **Per-invocation.** The ruleset is built fresh for each spawn, so a project
  switch moves the scope with no extra plumbing.
* **Monotonic.** Rulesets intersect, never replace. A child that calls
  ``landlock_restrict_self`` again with a *wider* set still cannot reach the
  addition, so a sub-agent is structurally incapable of widening its parent's
  scope — the same attenuation law ``AgentContext._narrow`` applies one layer up.

Landlock only ever *removes* access. Ordinary filesystem permissions still apply
underneath, which is why granting broadly outside the deny set costs nothing.

Absent or too-old kernel support degrades to today's behaviour with one log line.
A missing sandbox must never fail a run.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from loguru import logger
from mewbo_core.config import get_config_value

# Syscall numbers are stable across the architectures this ships on (x86_64,
# arm64). On anything else the probe simply fails and the feature degrades —
# a wrong number cannot mis-restrict, it can only fail to restrict.
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446

_PR_SET_NO_NEW_PRIVS = 38
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1

# Access bits paired with the ABI level that introduced them. Naming a bit the
# running kernel does not know is an EINVAL on ``create_ruleset``, so the
# handled set is always masked down to what this kernel actually supports.
_FS_BITS: dict[str, tuple[int, int]] = {
    "EXECUTE": (1 << 0, 1),
    "WRITE_FILE": (1 << 1, 1),
    "READ_FILE": (1 << 2, 1),
    "READ_DIR": (1 << 3, 1),
    "REMOVE_DIR": (1 << 4, 1),
    "REMOVE_FILE": (1 << 5, 1),
    "MAKE_CHAR": (1 << 6, 1),
    "MAKE_DIR": (1 << 7, 1),
    "MAKE_REG": (1 << 8, 1),
    "MAKE_SOCK": (1 << 9, 1),
    "MAKE_FIFO": (1 << 10, 1),
    "MAKE_BLOCK": (1 << 11, 1),
    "MAKE_SYM": (1 << 12, 1),
    "REFER": (1 << 13, 2),
    "TRUNCATE": (1 << 14, 3),
    "IOCTL_DEV": (1 << 15, 5),
}

# Creating a device node is the one grant Landlock cannot police afterwards:
# block-device I/O is not a path operation, so a root shell could `mknod` its way
# to a raw disk through a channel the ruleset never sees. Nothing an agent
# legitimately does needs them.
_UNGRANTED_BITS = ("MAKE_CHAR", "MAKE_BLOCK")


def _resolve(handle: ctypes.CDLL, name: str) -> Any:
    """Look a symbol up once, tolerating a platform that does not have it.

    ``prctl`` is Linux-only, and a bare attribute access would raise at IMPORT
    time on macOS or BSD — taking the whole shell toolchain down with it, since
    the tool registry imports this transitively. CI would stay green, because CI
    only runs the platform that does not trip it.
    """
    try:
        return getattr(handle, name)
    except (AttributeError, OSError):  # pragma: no cover - platform-dependent
        return None


_libc = ctypes.CDLL(None, use_errno=True)
_syscall = _resolve(_libc, "syscall")
if _syscall is not None:
    _syscall.restype = ctypes.c_long

# The child-side hook calls through a PyDLL handle, NOT the CDLL one. ``CDLL``
# releases the GIL around every call; ``PyDLL`` does not. The hook runs between
# fork and exec, where the child's GIL mutex is a copy of one another thread may
# have held at fork time — a drop/take cycle there can block forever on a lock
# with no owner. These syscalls do not block, so holding the GIL costs nothing.
_pylibc = ctypes.PyDLL(None, use_errno=True)
_child_syscall = _resolve(_pylibc, "syscall")
if _child_syscall is not None:
    _child_syscall.restype = ctypes.c_long
_child_prctl = _resolve(_pylibc, "prctl")

_PLATFORM_SUPPORTED = (
    sys.platform.startswith("linux")
    and _syscall is not None
    and _child_syscall is not None
    and _child_prctl is not None
)

# Pre-built so the child writes it without formatting or allocating. Without it a
# denied spawn is exit 127 and an EMPTY buffer — indistinguishable from "command
# not found", with nothing logged anywhere, because the only process that knows
# is the one about to die.
_REFUSAL_NOTICE = b"mewbo: landlock restrict_self failed; refusing to run unscoped\n"


class SandboxUnavailableError(OSError):
    """Landlock is present, was asked for a ruleset, and produced none.

    Distinct from a kernel that has no Landlock at all, which is a documented
    degradation every caller passes through as "spawn exactly as before". This
    is the case where the control WAS available and still yielded nothing, and
    the repo's own law applies: a filter that cannot be applied must refuse,
    never fall back to everything.

    **An ``OSError`` on purpose, and the base class is load-bearing.** It IS a
    failed syscall, and every spawn seam already wraps its ``Popen`` in
    ``except OSError`` to turn a failure into that seam's own graceful envelope
    — ``ShellSession`` into a ``_start_error`` the model can read. A bare
    ``RuntimeError`` here would sail past all of them, so refusing to spawn
    would surface as a traceback rather than as a refusal, and the caller that
    is supposed to act on it would be the one place it never legibly arrived.
    """


@dataclass(frozen=True, slots=True)
class RulesetBuild:
    """What compiling a :class:`ShellScope` into a kernel ruleset produced.

    Exists so a caller can tell "this kernel enforces nothing" from "confined,
    minus these paths" — one sentinel for both is what let a lost grant read as
    no sandbox. Plain frozen state: it crosses no trust boundary and lives for
    the length of one spawn.
    """

    fd: int | None
    ungranted: tuple[str, ...] = ()


class _RulesetAttr(ctypes.Structure):
    """``struct landlock_ruleset_attr`` — the access classes we take charge of."""

    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    """``struct landlock_path_beneath_attr`` — one allowed directory.

    Packed: the kernel expects 12 bytes, and natural alignment would pad it to 16
    and be rejected.
    """

    _pack_ = 1
    _fields_ = (
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    )


@dataclass(frozen=True, slots=True)
class LandlockAbi:
    """The running kernel's Landlock ABI level and the masks it will accept.

    Probed once per process and cached — the answer cannot change while the
    kernel is running.

    Cost: O(1).
    """

    version: int

    @property
    def available(self) -> bool:
        """Whether this kernel can enforce a filesystem ruleset at all."""
        return self.version >= 1

    def handled_access_fs(self) -> int:
        """Every filesystem access class this kernel understands."""
        return self._mask(_FS_BITS)

    def grant_access(self) -> int:
        """What a permitted directory gets: everything except device creation."""
        return self._mask(name for name in _FS_BITS if name not in _UNGRANTED_BITS)

    def _mask(self, names: Iterable[str]) -> int:
        """OR the named bits this ABI level actually accepts (they are disjoint)."""
        return sum(
            bit for bit, since in (_FS_BITS[n] for n in names) if self.version >= since
        )

    @staticmethod
    @lru_cache(maxsize=1)
    def probe() -> LandlockAbi:
        """Ask the kernel for its ABI level, logging an unusable one once."""
        if not _PLATFORM_SUPPORTED:
            logger.info(
                "Landlock is not available on this platform ({}); shell "
                "subprocesses run unscoped, exactly as before.",
                sys.platform,
            )
            return LandlockAbi(version=0)
        version = int(
            _syscall(
                ctypes.c_long(_SYS_CREATE_RULESET),
                ctypes.c_void_p(None),
                ctypes.c_size_t(0),
                ctypes.c_uint32(_CREATE_RULESET_VERSION),
            )
        )
        abi = LandlockAbi(version=max(version, 0))
        if not abi.available:
            logger.info(
                "Landlock is unavailable on this kernel (probe returned {}); "
                "shell subprocesses run unscoped, exactly as before.",
                version,
            )
        return abi


@dataclass(frozen=True, slots=True)
class ShellScope:
    """The directories a shell subprocess may NOT reach.

    Everything absent from :attr:`denied` stays reachable, subject to ordinary
    filesystem permissions. That is the inversion the module docstring argues
    for: this list is short and knowable, where its complement is every
    dependency of every command an agent might run.

    Cost: O(entries in the denied paths' parent directories) to compile.
    """

    denied: tuple[str, ...]
    allowed: tuple[str, ...] = ()

    @classmethod
    def for_active_root(cls, active_root: str | None) -> ShellScope | None:
        """Deny every configured project except the session's own, plus extras.

        *active_root* is the directory the session is working in — a resolved
        project, or its own scratch directory when no project is bound. It is
        supplied by the loop and never by the model, because a model-chosen root
        would be a model-chosen sandbox.

        Returns ``None`` when there is nothing to deny, which callers pass
        straight through as "spawn exactly as before".
        """
        if not bool(get_config_value("agent", "shell_sandbox", default=False)):
            return None
        denied, allowed = cls.denied_for(active_root)
        if not denied:
            return None
        # ``allowed`` is granted explicitly, because a denial cannot be relied
        # on to leave those paths reachable: a worktree lives INSIDE its
        # project, so denying the project denies the very directory the session
        # works in. Landlock needs no ancestor grant — a rule on the deep path
        # stands on its own — so this carves exactly the channel the session
        # needs and nothing wider.
        return cls(denied=denied, allowed=allowed)

    @classmethod
    def denied_for(
        cls, active_root: str | None, *, include_other_projects: bool = True
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The ``(denied, allowed)`` pair for *active_root*, with NO policy gate.

        The ONE derivation of "what may this session not reach", shared by both
        halves of the boundary: :meth:`for_active_root` compiles it into a
        Landlock ruleset for the shell's subprocesses, and
        ``mewbo_tools.core.resolve_safe_path`` refuses a path argument landing
        under it. A second walk of ``config.projects`` +
        ``agent.shell_denied_paths`` would be free to drift the moment either
        side gained a rule — which is precisely how an operator's
        ``shell_denied_paths`` came to be denied to the shell and allowed to
        ``read_file``.

        Deliberately UNGATED, because the two callers answer to different
        switches. ``agent.shell_sandbox`` governs whether a KERNEL mechanism
        applies at all, so a deployment on an old kernel turns it off; an
        operator's ``shell_denied_paths`` declaration is POLICY, not mechanism,
        and the path guard honours it under its own
        ``agent.path_scope_to_active_project``. Each caller applies its own
        gate before calling here.

        ``denied`` has already passed :meth:`_survivable`; ``allowed``
        (:meth:`readmitted_for`) wins over it, the same precedence Landlock's
        compiled grants give it.

        *include_other_projects* drops the "every configured project that is
        not the active one" component. It exists for the path guard's
        no-published-root case ONLY, where there is no active project and so
        every configured one would be "other": the shell falls closed there and
        denies them all, while the path guard must keep its historical union
        for direct library callers, tests and the CLI. That divergence is
        deliberate and named; the operator-declared and harness components are
        unaffected, which is what closes the paths that matter.

        Cost: O(configured projects).
        """
        active = os.path.realpath(active_root) if active_root else None
        # The active project re-admits its own extra paths; every other
        # project's are irrelevant, because the project itself is denied.
        readmitted = set(cls.readmitted_for(active_root))

        denied: set[str] = set()
        if include_other_projects:
            projects: dict = get_config_value("projects", default={}) or {}
            for cfg in projects.values():
                path = cls._entry(cfg, "path", "")
                if path and os.path.realpath(path) != active:
                    denied.add(os.path.realpath(path))

        extra_denied = get_config_value("agent", "shell_denied_paths", default=[]) or []
        denied.update(os.path.realpath(p) for p in extra_denied)
        denied.update(cls.harness_roots(active))

        # A re-admitted path wins: that is what makes `allowed_paths` able to
        # reach a sibling checkout that is itself a configured project.
        survivors = cls._survivable(denied - readmitted)
        return tuple(sorted(survivors)), tuple(sorted(readmitted))

    @staticmethod
    def harness_roots(active: str | None) -> set[str]:
        """Mewbo's own source and config directories, DERIVED rather than configured.

        Hiding the harness from the agent was the stated goal from the outset,
        and leaving it to ``shell_denied_paths`` meant the default shipped with
        the config that holds the API keys readable — measured on the deployment
        after everything else was scoped.

        Derivation rather than a constant, because the answer differs per
        deployment: a container runs from a source tree, a pip install runs from
        ``site-packages``. :meth:`_survivable` drops anything containing the
        runtime, so a pip install (where the packages ARE under ``sys.prefix``)
        contributes nothing — you cannot hide a harness that is also the
        interpreter.

        **The denial is unconditional, and *active* is accepted only to keep one
        signature across the shared derivation.** It used to drop any root lying
        inside *active*, so that working ON Mewbo kept its packages reachable —
        and on the shape that matters, where the whole checkout IS the active
        root, that erased the entire self-deny rather than narrowing it.
        ``agent.harness_self_deny`` replaces it: on by default, and off is the
        one-line opt-in for developing Mewbo itself. **A wrong carve-out is
        invisible on the box that matters, while a wrong denial is obvious on
        the box that does not** — deny-always fails loudly and locally, on a
        workstation, where the person hitting it fixes it in that one line.
        Three derivations were tried before landing here and none survives: "is
        the harness inside the active project" IS the one that just failed, "am
        I in a container" is inverted for the dev container, and "CLI vs API" is
        invisible to a scope object. One knob with a documented default beats a
        fourth guess.

        **Limit, stated so nobody over-reads it:** this denial is path-PREFIX
        based, so it cannot cover a second path to the same inodes. Measured on
        the deployed shape, where a local compose override bind-mounts the host
        checkout over the baked ``/app``: the same ``config.py`` is reachable as
        both ``/app/...`` and the host project path with identical ``st_dev`` +
        ``st_ino``, and ``os.path.realpath`` does NOT collapse a bind mount, so
        denying the ``/app`` prefix leaves the project path open. The cure is
        topology — do not bind-mount the source over the baked ``/app`` — not a
        wider denial. Do NOT reach for inode identity instead: it would deny a
        legitimate project mount, and a project mount must behave like any other
        project.
        """
        if not bool(get_config_value("agent", "harness_self_deny", default=True)):
            return set()

        import mewbo_core

        candidates: set[str] = set()
        package_dir = os.path.dirname(os.path.abspath(mewbo_core.__file__))
        # Walk out of `<root>/packages/mewbo_core/src/mewbo_core` to `<root>`,
        # then name the source trees. A layout without them contributes nothing.
        parts = package_dir.split(os.sep)
        if "packages" in parts:
            repo_root = os.sep.join(parts[: parts.index("packages")])
            for name in ("packages", "apps"):
                candidates.add(os.path.join(repo_root, name))
        try:
            from mewbo_core.config import _resolve_config_path

            candidates.add(os.path.dirname(os.path.realpath(_resolve_config_path("app.json"))))
        except Exception as exc:  # pragma: no cover - config layout is deployment-specific
            # WARNING, not DEBUG, and it names the consequence: a failure here
            # drops the directory holding the API keys out of the deny set, and
            # a trace below the default sink level makes that indistinguishable
            # from a boundary that held. It stays non-fatal because this is one
            # component of a set — the packages and apps roots still apply, and
            # the config layout is deployment-specific enough that refusing
            # would fail shells on a layout that was never in danger.
            logger.warning(
                "Could not derive the harness config directory ({}); the directory holding "
                "app.json is NOT denied to sandboxed shells. Name it in "
                "agent.shell_denied_paths to close it explicitly.",
                exc,
            )

        return {os.path.realpath(c) for c in candidates if c and os.path.isdir(c)}

    @classmethod
    def readmitted_for(cls, active_root: str | None) -> tuple[str, ...]:
        """The directories the session's OWN project keeps, *active_root* first.

        This is the ONE answer to "which configured project is this session's,
        and what does it re-admit" — shared with the argument-validating half of
        the same boundary (``mewbo_tools.core._get_allowed_roots``) so the two
        enforcement points cannot disagree about whose files a session may
        reach. A second walk of ``config.projects`` would be free to drift the
        moment either side gained a rule.

        A project's ``allowed_paths`` count only while that project IS the
        active one; every other project's are irrelevant, since the project
        itself is out of scope. A worktree needs no special case — it is
        published as the active root in its own right, and one living inside its
        project is covered by the project either way.

        Returns ``()`` for a falsy *active_root*, which each caller reads as
        "nothing was published", never as "nothing is allowed".

        Cost: O(configured projects).
        """
        if not active_root:
            return ()
        active = os.path.realpath(active_root)
        readmitted = [active]
        projects: dict = get_config_value("projects", default={}) or {}
        for cfg in projects.values():
            path = cls._entry(cfg, "path", "")
            if path and os.path.realpath(path) == active:
                readmitted.extend(
                    os.path.realpath(p) for p in cls._entry(cfg, "allowed_paths", [])
                )
        return tuple(dict.fromkeys(readmitted))

    @staticmethod
    def _entry(cfg: object, name: str, default: Any) -> Any:
        """Read *name* off a project entry, which config hands over as either shape."""
        if isinstance(cfg, dict):
            return cfg.get(name, default) or default
        return getattr(cfg, name, default) or default

    @staticmethod
    def _survivable(denied: Iterable[str]) -> set[str]:
        """Drop denials that would take the interpreter down with them.

        Denying a directory that CONTAINS the running runtime does not confine
        the agent, it breaks every command it could run — and the two are easy to
        conflate, because a deployment's harness source and its virtualenv often
        share a root (``/app`` here holds both the packages and ``/app/.venv``).
        Measured: denying that root leaves Python unable to start at all.

        The filesystem root is refused for the same reason, one level up. Both
        are misconfigurations, so they are logged rather than silently honoured —
        a sandbox that swallowed them would look identical to one that worked.
        """
        protected = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix) if p}
        kept: set[str] = set()
        for path in denied:
            if path == os.sep:
                logger.warning(
                    "Refusing to deny the filesystem root to a sandboxed shell; "
                    "it would deny the runtime too. Check agent.shell_denied_paths."
                )
                continue
            covering = [p for p in protected if p == path or p.startswith(path + os.sep)]
            if covering:
                logger.warning(
                    "Refusing to deny {!r} to a sandboxed shell: it contains the "
                    "Python runtime ({}), so denying it would stop every command "
                    "rather than confine it. Name the subdirectories to hide instead.",
                    path,
                    ", ".join(sorted(covering)),
                )
                continue
            kept.add(path)
        return kept

    @classmethod
    def for_spawned_server(cls, root: str | None) -> ShellScope | None:
        """The scope for a server we CONFIGURE but do not spawn ourselves.

        An MCP stdio server is spawned inside the adapter library and a
        language server inside ``pygls``, so neither seam can be handed a
        ``preexec_fn``. The command string is ours, though, which is what the
        launcher shim turns into an application point — see
        :mod:`mewbo_tools.integration.sandbox_launcher`.

        The policy is deliberately :meth:`for_active_root`'s and not a second
        one: the deny set stays a single object, and only the number of places
        it is applied grows. *root* is the workspace that server exists to
        serve, so it is re-admitted while every other configured project is
        denied.

        Gated on its own ``agent.server_sandbox`` **on top of**
        ``agent.shell_sandbox``, and that conjunction is the point rather than
        an oversight: this applies the very same kernel mechanism, so a
        deployment that turned the shell sandbox off — an older kernel, or a
        ruleset that broke a command — must not have it reappear underneath its
        language servers. The extra switch exists because the blast radius
        differs: a denied path here surfaces as a missing file inside a server
        that then reports nothing useful, and which servers a deployment runs
        is not knowable in advance.

        Returns ``None`` when either gate is off or there is nothing to deny,
        which every caller passes through as "launch exactly as before".

        Cost: O(configured projects).
        """
        if not bool(get_config_value("agent", "server_sandbox", default=False)):
            return None
        return cls.for_active_root(root)

    def grants(self) -> list[str]:
        """Compile the denials into the directories Landlock should permit.

        Landlock grants beneath a path, so an ancestor of a denied path can never
        be granted wholesale — it has to be expanded into the children that do
        not lead to a denial. Walking only those ancestors is what keeps this
        proportional to the denied set rather than to the filesystem.

        :attr:`allowed` is then added on top, so a path that sits UNDER a denial
        is still reachable. That is not an edge case: a session's worktree lives
        inside its project, so the expansion alone would leave the session unable
        to read the directory it works in.
        """
        blocked = {os.sep}
        for path in self.denied:
            parent = os.path.dirname(path)
            while parent and parent != os.sep:
                blocked.add(parent)
                parent = os.path.dirname(parent)

        denied = set(self.denied)
        permitted: list[str] = []
        for parent in sorted(blocked):
            try:
                entries = os.listdir(parent)
            except OSError:
                continue  # unreadable to us anyway, so nothing to grant beneath
            for name in entries:
                child = os.path.join(parent, name)
                if child in denied or child in blocked:
                    continue  # denied outright, or expanded at its own level
                if os.path.isdir(child) and not os.path.islink(child):
                    permitted.append(child)
        permitted.extend(p for p in self.allowed if p and os.path.isdir(p))
        return permitted

    # The ``isdir`` filters above are a CORRECTNESS gate, not a security one,
    # and they stay. Naming a directory-only access bit on a regular file is an
    # EINVAL (see :meth:`_add_rule`), so without them every regular file in an
    # expanded ancestor would reach the add-rule path and fail there. They do
    # open a window — a path can vanish between the filter and the open — but
    # that window is no longer load-bearing: :meth:`_create_ruleset` drops the
    # one path rather than the ruleset, so losing the race costs a grant and
    # never the sandbox. Removing the filter would trade a harmless race for a
    # guaranteed failure on every file.

    @contextmanager
    def enforced(self) -> Iterator[Callable[[], None] | None]:
        """Yield a ``preexec_fn`` that confines the child to this scope.

        The ruleset is created and populated **in the parent**; only the
        two-syscall ``restrict_self`` half runs in the child. A ``preexec_fn``
        runs between fork and exec in a process whose other threads may have held
        locks at fork time, so the hook does as little as it can — every argument
        is a ``ctypes`` value built here and the ruleset arrives as an inherited
        file descriptor.

        It minimises that window rather than eliminating it: calling any Python
        callable from ``preexec_fn`` still builds an argument tuple, a frame and a
        result, so a gen-0 GC pass stays reachable. Removing the last of it would
        mean giving up ``preexec_fn`` as the seam.

        Yields ``None`` when the kernel cannot enforce anything, which callers
        pass to ``Popen`` unchanged to get today's behaviour. That is the ONLY
        thing ``None`` means here. A kernel that HAS Landlock and still refused
        the ruleset raises :exc:`SandboxUnavailableError` instead of yielding,
        because a shell is not worth spawning on the terms it would then get: a
        model-authored command has no claim to run, and the operator believing
        it confined is the whole failure. A grant that could not be added is
        neither — it narrows the scope and the shell still runs, since a
        transient path error must not be able to fail a run either.
        """
        build = self._create_ruleset()
        if build.fd is None:
            yield None
            return
        try:
            yield self._child_hook(build.fd)
        finally:
            os.close(build.fd)

    def apply_to_self(self) -> bool:
        """Confine THIS process — and everything it goes on to ``exec`` — here.

        The self-application half of :meth:`enforced`, for a spawn seam that
        lives inside a third-party library and so has no ``preexec_fn`` to take.
        A shim process applies the ruleset to itself and then replaces itself
        with the real command; the ruleset survives ``execve``, which is the
        whole reason the shim works.

        It shares :meth:`_create_ruleset` with :meth:`enforced` and differs only
        in what a failure MEANS. The child hook kills a shell it could not
        confine, because failing open there hands the model an unscoped shell
        while the operator believes otherwise. Here the alternative to running
        unconfined is a language server or MCP server that never starts at all,
        so the failure is reported and the caller decides — and every caller in
        this package chooses to launch. Read ``False`` as "unconfined", never as
        "unchanged".

        The two syscalls go through the ``PyDLL`` handles even though nothing
        here sits between fork and exec: neither blocks, so holding the GIL
        costs nothing, and one resolution path is one fewer thing to get wrong.

        Cost: O(entries in the denied paths' parent directories).
        """
        try:
            build = self._create_ruleset()
        except SandboxUnavailableError as exc:
            # The shell's half re-raises this; here it is caught and reported,
            # for the reason the docstring above gives — the alternative to an
            # unconfined server is no server, and that decision is the
            # caller's, not this method's.
            logger.warning("{} This process runs unscoped.", exc)
            return False
        fd = build.fd
        if fd is None:
            return False
        try:
            if (
                _child_prctl(
                    ctypes.c_int(_PR_SET_NO_NEW_PRIVS),
                    ctypes.c_ulong(1),
                    ctypes.c_ulong(0),
                    ctypes.c_ulong(0),
                    ctypes.c_ulong(0),
                )
                != 0
            ):
                logger.warning(
                    "Landlock no_new_privs failed (errno {}); this process runs unscoped.",
                    ctypes.get_errno(),
                )
                return False
            if (
                _child_syscall(
                    ctypes.c_long(_SYS_RESTRICT_SELF),
                    ctypes.c_int(fd),
                    ctypes.c_uint32(0),
                )
                != 0
            ):
                logger.warning(
                    "Landlock restrict_self failed (errno {}); this process runs unscoped.",
                    ctypes.get_errno(),
                )
                return False
            return True
        finally:
            os.close(fd)

    def _create_ruleset(self) -> RulesetBuild:
        """Create the ruleset and add every compiled grant.

        Three outcomes, and conflating any two of them is how this became a
        security defect. They are distinguished in the return value rather than
        in a log line, because the process that has to act on the difference is
        the caller, and a log sink is not a return channel.

        * **The kernel has no Landlock** — nothing to enforce anywhere, so the
          build is empty and every caller spawns exactly as before. Documented
          degradation; a missing sandbox must never fail a run.
        * **Landlock is present and the ruleset was still refused** — the control
          was asked for and produced nothing. :exc:`SandboxUnavailableError`, so
          the caller decides rather than silently receiving no sandbox.
        * **One grant could not be added** — the path is dropped and the build
          continues. This can only ever make the scope NARROWER, so it is the
          safe direction by construction.

        Cost: O(compiled grants), one syscall each.
        """
        abi = LandlockAbi.probe()
        if not abi.available:
            return RulesetBuild(fd=None)
        attr = _RulesetAttr(handled_access_fs=abi.handled_access_fs(), handled_access_net=0)
        # ABI 4 grew the network field; an older kernel is handed the shorter
        # struct it knows about.
        size = ctypes.sizeof(_RulesetAttr) if abi.version >= 4 else 8
        fd = int(
            _syscall(
                ctypes.c_long(_SYS_CREATE_RULESET),
                ctypes.byref(attr),
                ctypes.c_size_t(size),
                ctypes.c_uint32(0),
            )
        )
        if fd < 0:
            raise SandboxUnavailableError(
                f"Landlock ruleset creation failed (errno {ctypes.get_errno()}) on a kernel "
                "that supports it; refusing to continue unscoped."
            )
        access = abi.grant_access()
        # Compiled once: `grants()` is a `listdir` per expanded ancestor, so
        # re-deriving it to count the failures would double the compile.
        compiled = self.grants()
        ungranted: list[str] = []
        for path in compiled:
            # Per PATH, never per ruleset. Letting one failure out of this loop
            # is what turned a transient path error — a directory removed by
            # another process, a mount going away, a permissions flap, an fd
            # limit — into a shell with no sandbox at all.
            try:
                self._add_rule(fd, path, access)
            except OSError as exc:
                ungranted.append(path)
                logger.debug("Landlock could not grant {!r} ({}); it stays unreachable.", path, exc)
        if ungranted:
            # One line for the whole build: a per-path warning on a compile that
            # is ~95 rules on a real deployment shape would bury its own signal.
            logger.warning(
                "Landlock granted {} of {} paths; this shell IS confined, and these stay "
                "unreachable to it: {}. A path the session needs here surfaces as a "
                "permission error inside the shell, not as a refusal to start.",
                len(compiled) - len(ungranted),
                len(compiled),
                ", ".join(sorted(ungranted)),
            )
        return RulesetBuild(fd=fd, ungranted=tuple(ungranted))

    @staticmethod
    def _add_rule(fd: int, path: str, access: int) -> None:
        """Permit *access* beneath *path*.

        Regular files are filtered out before they reach here (see
        :meth:`grants`): naming a directory-only access bit on a file is an
        EINVAL, and every file worth granting is covered by its parent.

        Raises ``OSError``, which :meth:`_create_ruleset` catches per PATH — one
        refused rule costs that grant and nothing else. It used to fail the whole
        ruleset, which meant an unconfined shell.
        """
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = _PathBeneathAttr(allowed_access=access, parent_fd=parent_fd)
            rc = int(
                _syscall(
                    ctypes.c_long(_SYS_ADD_RULE),
                    ctypes.c_int(fd),
                    ctypes.c_int(_RULE_PATH_BENEATH),
                    ctypes.byref(rule),
                    ctypes.c_uint32(0),
                )
            )
        finally:
            os.close(parent_fd)
        if rc != 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")

    @staticmethod
    def _child_hook(fd: int) -> Callable[[], None]:
        """Build the between-fork-and-exec hook that applies the ruleset.

        Every ``ctypes`` argument is constructed here, in the parent. The returned
        closure performs two syscalls and nothing else.
        """
        c_fd = ctypes.c_int(fd)
        c_flags = ctypes.c_uint32(0)
        c_restrict = ctypes.c_long(_SYS_RESTRICT_SELF)
        c_nnp = ctypes.c_int(_PR_SET_NO_NEW_PRIVS)
        c_one = ctypes.c_ulong(1)
        c_zero = ctypes.c_ulong(0)

        def _restrict_self() -> None:
            # Failing open here would hand the model an unscoped shell while the
            # operator believes it is confined, so a failure kills the child. It
            # says so on fd 2 first: the parent's ``Popen`` succeeds either way,
            # so silence would read as a broken toolbox rather than a broken
            # sandbox. ``os.write`` of a module constant allocates nothing.
            if _child_prctl(c_nnp, c_one, c_zero, c_zero, c_zero) != 0:
                os.write(2, _REFUSAL_NOTICE)
                os._exit(127)
            if _child_syscall(c_restrict, c_fd, c_flags) != 0:
                os.write(2, _REFUSAL_NOTICE)
                os._exit(127)

        return _restrict_self


def scoped_preexec(scope: ShellScope | None) -> AbstractContextManager[Callable[[], None] | None]:
    """The one ``with ... as hook: Popen(..., preexec_fn=hook)`` composition.

    Every non-shell-session spawn site that wants Landlock confinement calls
    :meth:`ShellScope.for_active_root` for its own active root, then wraps the
    result here — a *scope* of ``None`` (flag off, nothing survived compilation,
    Landlock unavailable) is ``nullcontext(None)``, which callers pass to
    ``Popen``/``subprocess.run`` unchanged to spawn unscoped. Extracted so the
    other confined spawn seams (``pipeline_runner.PipelineExecutor``, the skills
    ``!`command`` preprocessor, the wiki clone executor) share ONE spelling of
    this composition rather than re-deriving it; ``ShellSession`` calls it too.

    **The scip-python resolver is NOT among them, and that is deliberate** —
    ``wiki/resolve/scip_python.py`` spawns unconfined because a scope limited to
    the project root hides the interpreter's ``site-packages``, which fails
    scip-python's own dependency probe and drops every cross-file edge. It is a
    first-party read-only indexer whose argv we build, not a model-authored
    command. Do not "finish the job" by wrapping it here.
    """
    return scope.enforced() if scope is not None else nullcontext(None)


def shell_preexec_for_root(
    active_root: str | None,
) -> AbstractContextManager[Callable[[], None] | None]:
    """Build the confining scope for *active_root* and hand back its spawn hook.

    The concrete implementation registered into
    ``mewbo_core.workspaces.workspace.register_shell_preexec_factory`` — see the
    self-registration at the bottom of this module for why core needs a pushed
    seam here rather than every caller importing this package directly.
    """
    return scoped_preexec(ShellScope.for_active_root(active_root))


def _register_with_core() -> None:
    """Push :func:`shell_preexec_for_root` down into core's registration seam.

    Both ``mewbo_core`` (the skills ``!`command`` preprocessor) and
    ``mewbo_graph`` (the git executor, the scip-python resolver) want the SAME
    Landlock confinement every other spawn seam gets, but NEITHER may import
    ``mewbo_tools``: root CLAUDE.md refuses a 13th function-local reach-up out
    of core (the cure being a registration seam pushed DOWN, never one more
    guarded import), and ``mewbo_graph``'s own CLAUDE.md is exactly as absolute
    — it "imports down into core + pydantic and NOTHING ELSE — never
    mewbo_tools". So core defines ``register_shell_preexec_factory`` as one
    inert down-only seam that serves every consumer above it in the DAG, and
    this package — which already legitimately imports core, the ordinary
    direction — pushes its factory into it at ITS OWN import time, mirroring
    ``mewbo_graph``'s ``register_builtin_plugins()`` self-registration on
    import. A core-only install that never imports this package leaves the
    seam unregistered, and every caller of ``shell_preexec_scope`` degrades to
    an unscoped spawn — exactly today's behaviour, never a failure.
    """
    from mewbo_core.workspaces.workspace import register_shell_preexec_factory

    register_shell_preexec_factory(shell_preexec_for_root)


_register_with_core()
