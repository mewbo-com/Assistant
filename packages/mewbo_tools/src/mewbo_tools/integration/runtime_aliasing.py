"""Detect that the harness is reachable by a second path, and say so at boot.

Every path-based control this package ships — Landlock's compiled grants and
``resolve_safe_path``'s denied roots alike — denies by path PREFIX. A bind mount
gives the same inodes a second name, and ``os.path.realpath`` does NOT collapse
one, because it is not a symlink. So a denial on the harness's own installed
path says nothing about the same files reached through a project mount, and the
control reports healthy while enforcing nothing.

Measured on a real deployment: a dev-loop compose override bind-mounts the host
checkout over the baked runtime directory, the checkout is also a configured
project, and the turn loop's source plus the file holding the provider
credentials are refused by one path and resolved for WRITE by the other. Same
``st_dev``, same ``st_ino``, both times.

**The cure is topology, not a wider denial**, and the tempting patch is wrong in
a way worth writing down: denying by inode identity instead of by prefix would
deny the legitimate *project* mount too, and a project mount must behave like
any other project — editable, and with no path to the running runtime. Fixing
this means not mounting the source over the runtime in the first place, which is
a deployment decision no code here can make.

What code CAN do is refuse to let it be silent. This module compares the two
sides at boot and logs loudly when they are the same files, which turns an
invisible bypass into a line in the log that names both paths.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from loguru import logger
from mewbo_core.config import get_config_value

from mewbo_tools.integration.landlock import ShellScope

# The deepest a bind mount has been seen to land below a harness root: the
# deployed shape mounts `<root>/packages/<pkg>/src/<pkg>`, which is four levels
# down. Bounded because this walks directories at boot, and an unbounded walk of
# a project tree is not a startup cost anyone should pay.
_DEFAULT_MAX_DEPTH = 4


@dataclass(frozen=True, slots=True)
class RuntimeAlias:
    """One harness directory found reachable under a configured project."""

    harness_path: str
    project_path: str
    device: int
    inode: int

    def describe(self) -> str:
        """One line naming both names of the same directory."""
        return (
            f"{self.harness_path!r} is the same directory as {self.project_path!r} "
            f"(dev={self.device}, ino={self.inode})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeAliasProbe:
    """Whether the harness is reachable by a path no harness denial covers.

    Collaborators arrive as fields so this is testable without real mounts: the
    two directory readers are what a test replaces to describe an aliased shape
    that does not exist on the machine running the test.

    Cost: O(directories within *max_depth* of the harness roots and of each
    configured project root). Paid once, at startup, never on a request path.
    """

    harness_roots: tuple[str, ...]
    project_roots: tuple[str, ...]
    max_depth: int = _DEFAULT_MAX_DEPTH
    identify: Callable[[str], tuple[int, int]] = field(
        default=lambda path: (os.stat(path).st_dev, os.stat(path).st_ino)
    )
    list_dir: Callable[[str], list[str]] = field(default=os.listdir)
    # Injected with the other two readers rather than called inline: a
    # filesystem predicate buried in the walk makes the walk untestable without
    # a real tree, which for this feature means real bind mounts.
    is_walkable: Callable[[str], bool] = field(
        default=lambda path: os.path.isdir(path) and not os.path.islink(path)
    )

    @classmethod
    def for_deployment(cls, *, max_depth: int = _DEFAULT_MAX_DEPTH) -> RuntimeAliasProbe:
        """Build the probe from the same derivations the denials themselves use.

        ``ShellScope.harness_roots`` is the ONE answer to "what is the harness",
        so asking it here keeps the probe honest: it can only ever report on the
        roots that are actually being denied. A second derivation would be free
        to drift and start reporting on a directory nothing protects.
        """
        projects: dict = get_config_value("projects", default={}) or {}
        roots = []
        for cfg in projects.values():
            path = cfg.get("path", "") if isinstance(cfg, dict) else getattr(cfg, "path", "")
            if path:
                roots.append(os.path.realpath(path))
        return cls(
            harness_roots=tuple(sorted(ShellScope.harness_roots(None))),
            project_roots=tuple(sorted(set(roots))),
            max_depth=max_depth,
        )

    def aliases(self) -> tuple[RuntimeAlias, ...]:
        """Every harness directory that is also reachable under a project root."""
        harness = self._identify_tree(self.harness_roots)
        if not harness:
            return ()
        found: list[RuntimeAlias] = []
        for identity, project_path in self._identify_tree(self.project_roots).items():
            harness_path = harness.get(identity)
            if harness_path is None or harness_path == project_path:
                continue
            found.append(
                RuntimeAlias(
                    harness_path=harness_path,
                    project_path=project_path,
                    device=identity[0],
                    inode=identity[1],
                )
            )
        return tuple(sorted(found, key=lambda a: a.project_path))

    def report(self) -> tuple[RuntimeAlias, ...]:
        """Log every alias found, loudly, and hand them back to the caller.

        Returns rather than only logging, so a caller that wants to refuse to
        start — or a test — reads the finding instead of scraping a log sink.
        """
        found = self.aliases()
        for alias in found:
            logger.warning(
                "Harness self-deny cannot hold: {}. The harness is denied by PATH PREFIX and "
                "a bind mount is not a symlink, so a denial on the first name leaves the "
                "second one open — a session bound to that project reaches the running "
                "harness source and its credentials. Stop mounting the source over the "
                "runtime; widening the denial would deny a legitimate project mount.",
                alias.describe(),
            )
        return found

    def _identify_tree(self, roots: Iterable[str]) -> dict[tuple[int, int], str]:
        """Map ``(st_dev, st_ino)`` to a path for every directory under *roots*.

        First path wins for a given identity, which only matters within ONE side
        of the comparison: two names for the same directory inside the harness
        tree are not what this looks for.
        """
        seen: dict[tuple[int, int], str] = {}
        for root in roots:
            self._walk(root, depth=0, into=seen)
        return seen

    def _walk(self, path: str, *, depth: int, into: dict[tuple[int, int], str]) -> None:
        """Record *path*'s identity and recurse until *max_depth*.

        Symlinked and unreadable entries are skipped: a symlink IS collapsed by
        ``realpath`` and so is already covered by the prefix denial, and a
        directory this process cannot read is one it cannot alias through.
        """
        try:
            identity = self.identify(path)
        except OSError:
            return
        into.setdefault(identity, path)
        if depth >= self.max_depth:
            return
        try:
            entries = self.list_dir(path)
        except OSError:
            return
        for name in entries:
            child = os.path.join(path, name)
            if not self.is_walkable(child):
                continue
            self._walk(child, depth=depth + 1, into=into)
