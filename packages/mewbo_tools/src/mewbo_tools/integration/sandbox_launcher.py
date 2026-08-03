"""A launcher shim for the servers Mewbo configures but does not spawn.

``ShellScope`` confines the shell because the shell tool owns its own
``Popen`` call and can hand it a ``preexec_fn``. Two long-lived server kinds
own no such call:

* an **MCP stdio server** is spawned inside the adapter library, from the
  ``command``/``args`` we hand it;
* a **language server** is spawned inside ``pygls``' ``start_io``.

Neither library takes a ``preexec_fn``, so the seam is unreachable — but *the
command string is ours*. That turns the spawn into an application point after
all: prefix the configured command with a shim that applies the ruleset to
ITSELF and then ``exec``s the real server, which inherits the confinement
because a Landlock ruleset survives ``execve``. One scope object, one more
place it is applied; no second policy.

The shim reads its scope from the environment rather than from argv. A path
list in argv has to survive two quoting layers (ours, then whatever the server
does with its own arguments), and the real command's argv is left byte-identical
this way — the server sees exactly the arguments the operator configured.

**The environment contract**, in one place because both wiring sites and the
shim itself depend on it:

``MEWBO_SANDBOX_SCOPE`` holds a JSON object ``{"denied": [...], "allowed": [...]}``
— absolute directory paths, exactly the two fields of :class:`ShellScope`. An
absent, empty or unparseable value means "apply nothing", never "deny
everything".

**It degrades at every step and never fails a launch.** No Landlock, no
variable, malformed JSON, a ruleset the kernel refuses — each writes one line to
stderr and ``exec``s the real command anyway. That is the opposite of the shell
tool's child hook, which kills a shell it could not confine, and the asymmetry is
deliberate: a model-authored shell command has no claim to run, whereas a server
that fails to start takes tool discovery or editor diagnostics down wholesale for
a confinement that is defence in depth. It also means confinement HERE is
best-effort — read a silent success as unproven, not as enforced.

Nothing is ever written to stdout: for both server kinds stdout IS the protocol
channel, and one stray byte on it is a parse error at the other end.

**Cost: one interpreter start per server START, measured at ~0.2s** — almost all
of it importing this module's way to a ``ShellScope``, with the ruleset itself
about a millisecond to compile and apply. It is paid once when a server is
launched and never on a tool call, which is the only reason it is acceptable;
anything that made this run per REQUEST would not be.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import ClassVar

from mewbo_tools.integration.landlock import ShellScope

# The console script declared in this package's ``[project.scripts]``. Resolved
# on PATH when present, with an interpreter-relative fallback for a checkout
# that has not been reinstalled since the entry point was added.
_CONSOLE_SCRIPT = "mewbo-sandbox-exec"


@dataclass(frozen=True, slots=True)
class SandboxLauncher:
    """The argv prefix and environment that confine one launched server.

    Built from a :class:`ShellScope`, so the policy is the shell sandbox's and
    only the application point is new. Construct it with :meth:`for_root`,
    which returns ``None`` whenever the launch must stay exactly as it is
    today — every caller reads that as "spawn unchanged" rather than branching
    on config itself.

    Cost: O(1) per launch; the scope it carries was compiled once by
    ``ShellScope.for_spawned_server``.
    """

    scope: ShellScope

    #: The environment variable carrying the JSON scope to the shim.
    ENV_VAR: ClassVar[str] = "MEWBO_SANDBOX_SCOPE"

    @classmethod
    def for_root(cls, root: str | None) -> SandboxLauncher | None:
        """A launcher scoped to *root*, or ``None`` to launch unchanged.

        *root* is the workspace the server about to be spawned exists to serve.
        It is re-admitted; every other configured project is denied.
        """
        scope = ShellScope.for_spawned_server(root)
        return None if scope is None else cls(scope=scope)

    def command(self, argv: Sequence[str]) -> list[str]:
        """*argv* prefixed with the shim, which ``exec``s it after confining."""
        return [*self._shim_argv(), *argv]

    def environ(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """*base* plus the scope the shim reads, as a new dict.

        *base* is copied rather than mutated: the caller's mapping is often
        ``os.environ`` or a config-owned dict, and neither may gain an entry
        because one server was launched.
        """
        env = dict(base or {})
        env[self.ENV_VAR] = json.dumps(
            {"denied": list(self.scope.denied), "allowed": list(self.scope.allowed)}
        )
        return env

    @staticmethod
    @lru_cache(maxsize=1)
    def _shim_argv() -> tuple[str, ...]:
        """How to invoke the shim, resolved once per process.

        The console script is preferred because it is one ``exec`` with no
        argument parsing, but it only exists once the package has been
        installed with this entry point declared. ``python -m`` reaches the
        same ``main`` from the running interpreter and needs no reinstall, so a
        checkout without the console script still launches.
        """
        found = shutil.which(_CONSOLE_SCRIPT)
        if found:
            return (found,)
        return (sys.executable, "-m", __spec__.name if __spec__ else __name__)

    @classmethod
    def apply_from_environ(cls) -> bool:
        """Confine the CURRENT process to the scope named in the environment.

        Returns whether a ruleset was actually applied. Every failure path
        returns ``False`` after one line on stderr — see the module docstring
        for why this degrades rather than refusing to continue.

        Cost: O(entries in the denied paths' parent directories).
        """
        raw = os.environ.get(cls.ENV_VAR)
        if not raw:
            return False
        try:
            payload = json.loads(raw)
            scope = ShellScope(
                denied=tuple(payload["denied"]) if payload.get("denied") else (),
                allowed=tuple(payload["allowed"]) if payload.get("allowed") else (),
            )
        # ``AttributeError`` is in here for a real shape, not for tidiness: a
        # JSON document that parses to a list or a string has no ``.get``.
        except (AttributeError, TypeError, ValueError) as exc:
            cls._notify(f"ignoring an unreadable {cls.ENV_VAR}: {exc}")
            return False
        try:
            applied = scope.apply_to_self()
        except Exception as exc:  # pragma: no cover - ctypes/kernel edge
            cls._notify(f"could not apply the sandbox: {exc}")
            return False
        if not applied:
            cls._notify("the sandbox was not applied; this server runs unconfined")
        return applied

    @staticmethod
    def _notify(message: str) -> None:
        """Say something on stderr, which is never a server's protocol channel."""
        sys.stderr.write(f"{_CONSOLE_SCRIPT}: {message}\n")
        sys.stderr.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """Confine this process, then become the command named in *argv*.

    The console-script entry point. Returns only when the ``exec`` could not
    happen at all — on success this process ceases to exist and the real
    server's own exit code is the one the parent sees.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        SandboxLauncher._notify("usage: mewbo-sandbox-exec COMMAND [ARG...]")
        return 2
    SandboxLauncher.apply_from_environ()
    # Dropped before the exec: the ruleset itself is what every descendant
    # inherits, so leaving the variable set would only invite a nested shim to
    # re-apply a scope the kernel already enforces.
    os.environ.pop(SandboxLauncher.ENV_VAR, None)
    try:
        os.execvp(args[0], args)
    except OSError as exc:
        SandboxLauncher._notify(f"cannot execute {args[0]!r}: {exc}")
        return 127
    return 127  # pragma: no cover - execvp either replaces us or raises


if __name__ == "__main__":  # pragma: no cover - the `python -m` fallback
    raise SystemExit(main())
