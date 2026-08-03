#!/usr/bin/env python3
"""Tests for the model-to-process seam in ``CommandVerifierRunner``.

Unlike ``test_verifier_gate.py`` — which drives the loop with a recording FAKE
runner and spawns nothing — this suite exercises the REAL runner and real
subprocesses, because the property under test is what the child process can
reach. An injected fake would prove nothing about the default runner (tests/
CLAUDE.md → "An injected-factory suite proves nothing about the DEFAULT
factory").

Both halves of the spawn are model-supplied via ``spawn_agent``'s ``checks[]``:
``argv`` and ``cwd``. The bound on ``cwd`` is arithmetic and always applies; the
Landlock confinement rides the ``shell_preexec_scope`` registration seam and is
absent on a core-only install, so the fake-factory tests register their OWN
factory and observe the hook FROM THE CHILD rather than asserting on the
parent's arguments (which cannot tell "confined" from "argument passed").

``TestRealLandlockEnforcement`` closes the gap a fake factory structurally
cannot: it registers the REAL ``mewbo_tools`` factory and asserts what the
kernel does to a verifier child. Both halves are asserted there — a sibling
project is unreadable AND a file inside the workspace still is, because a
denial-only test passes just as well when the subprocess never started.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import mewbo_core.workspaces.workspace as _workspace_mod
import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.contracts.verification import CommandVerification, CommandVerifierRunner
from mewbo_tools.integration import landlock
from mewbo_tools.integration.landlock import ShellScope

# Reused rather than re-derived: ``test_shell_landlock.py`` owns the one
# availability probe (``sys.platform`` + ``LandlockAbi.probe()``), and a second
# spelling of it here would drift. Where it skips, so does this file — a skip is
# honest about an absent kernel feature; a silent pass is not.
from test_shell_landlock import requires_landlock


@pytest.fixture(autouse=True)
def _isolate_preexec_factory() -> Iterator[None]:
    """Snapshot + restore the process-wide preexec factory.

    ``mewbo_tools.integration.landlock`` registers the real Landlock factory at
    ITS import time, and the full suite imports it. Left in place these tests
    would spawn under a real kernel ruleset — load-bearing for nothing here and
    a source of platform-dependent flakiness — and a test that registered a fake
    would leak it into every later suite.
    """
    saved = _workspace_mod._shell_preexec_factory
    _workspace_mod.reset_shell_preexec_factory()
    try:
        yield
    finally:
        _workspace_mod._shell_preexec_factory = saved


def _run(spec: CommandVerification, *, cwd: str | None, roots: tuple[str, ...] = ()):
    """Drive the real runner once and hand back its raw ``VerifierResult``."""
    runner = CommandVerifierRunner(allowed_roots=roots)
    return asyncio.run(runner.run(spec, cwd=cwd))


# ---------------------------------------------------------------------------
# The cwd bound — model text may not choose where the process starts
# ---------------------------------------------------------------------------


def test_cwd_outside_every_allowed_root_is_refused(tmp_path):
    """A model-supplied ``cwd`` outside the workspace never becomes a spawn."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    witness = elsewhere / "ran"

    spec = CommandVerification(
        argv=["/bin/sh", "-c", f"touch {witness}"], cwd=str(elsewhere)
    )
    result = _run(spec, cwd=str(workspace))

    assert result.error is not None
    assert str(elsewhere) in result.error
    assert result.exit_code == -1
    # The refusal is a REFUSAL, not a rewrite: nothing ran anywhere.
    assert not witness.exists()


def test_cwd_escaping_via_symlink_is_refused(tmp_path):
    """``realpath`` first — a symlink inside the workspace cannot step out."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (workspace / "out").symlink_to(elsewhere)

    spec = CommandVerification(argv=["/bin/true"], cwd=str(workspace / "out"))
    result = _run(spec, cwd=str(workspace))

    assert result.error is not None
    assert "outside the allowed roots" in result.error


def test_cwd_inside_the_workspace_still_runs(tmp_path):
    """The legitimate case is unchanged — a nested cwd resolves and executes."""
    workspace = tmp_path / "workspace"
    nested = workspace / "pkg"
    nested.mkdir(parents=True)
    (nested / "marker.txt").write_text("here", encoding="utf-8")

    spec = CommandVerification(argv=["/bin/cat", "marker.txt"], cwd=str(nested))
    result = _run(spec, cwd=str(workspace))

    assert result.error is None
    assert result.exit_code == 0
    assert result.stdout.strip() == "here"


def test_cwd_defaults_to_the_callers_own_directory(tmp_path):
    """No ``spec.cwd`` means the loop-published cwd, used verbatim."""
    (tmp_path / "marker.txt").write_text("caller", encoding="utf-8")

    result = _run(CommandVerification(argv=["/bin/cat", "marker.txt"]), cwd=str(tmp_path))

    assert result.error is None
    assert result.stdout.strip() == "caller"


def test_a_model_cwd_with_no_root_to_check_against_is_refused(tmp_path):
    """Fail CLOSED: an unappliable filter refuses rather than honouring the value."""
    witness = tmp_path / "ran"
    spec = CommandVerification(
        argv=["/bin/sh", "-c", f"touch {witness}"], cwd=str(tmp_path)
    )

    result = _run(spec, cwd=None)

    assert result.error is not None
    assert "no allowed root" in result.error
    assert not witness.exists()


def test_an_injected_allowed_root_admits_a_cwd_outside_the_caller_cwd(tmp_path):
    """The DI seam is real: a constructor-given root widens the bound, model text cannot."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    (second / "marker.txt").write_text("second", encoding="utf-8")

    spec = CommandVerification(argv=["/bin/cat", "marker.txt"], cwd=str(second))

    refused = _run(spec, cwd=str(workspace))
    assert refused.error is not None

    admitted = _run(spec, cwd=str(workspace), roots=(str(second),))
    assert admitted.error is None
    assert admitted.stdout.strip() == "second"


# ---------------------------------------------------------------------------
# Non-regression — the two properties that were already true
# ---------------------------------------------------------------------------


def test_argv_is_never_shell_interpreted(tmp_path):
    """``shell=True`` is never used, so metacharacters are literal arguments."""
    witness = tmp_path / "pwned"

    spec = CommandVerification(argv=["/bin/echo", f"hi; touch {witness}"])
    result = _run(spec, cwd=str(tmp_path))

    assert result.exit_code == 0
    assert result.stdout.strip() == f"hi; touch {witness}"
    assert not witness.exists()


def test_the_environment_is_scrubbed(tmp_path, monkeypatch):
    """A secret in the parent's environment does not reach the verifier."""
    monkeypatch.setenv("MEWBO_TEST_VERIFIER_SECRET", "swordfish")

    probe = "import os; print(os.environ.get('MEWBO_TEST_VERIFIER_SECRET'))"
    spec = CommandVerification(argv=[sys.executable, "-c", probe])
    result = _run(spec, cwd=str(tmp_path))

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "None"


# ---------------------------------------------------------------------------
# The confinement seam — observed from the child, not from the parent
# ---------------------------------------------------------------------------


def test_the_registered_scope_hook_runs_inside_the_child(tmp_path):
    """The ``preexec_fn`` from ``shell_preexec_scope`` executes post-fork.

    Asserting the parent passed a hook cannot distinguish "confined" from
    "argument passed", so the fake hook records the pid of the process it runs
    in and the verifier command reads that record back: the pid must be the
    CHILD's, not this interpreter's.
    """
    marker = tmp_path / "hook-pid"
    seen_roots: list[str | None] = []

    def _factory(active_root: str | None):
        seen_roots.append(active_root)

        @contextmanager
        def _cm() -> Iterator[Callable[[], None] | None]:
            def _hook() -> None:
                # Runs between fork and exec, in the child.
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))

            yield _hook

        return _cm()

    _workspace_mod.register_shell_preexec_factory(_factory)

    spec = CommandVerification(argv=["/bin/cat", str(marker)])
    result = _run(spec, cwd=str(tmp_path))

    assert result.error is None, result.error
    assert result.exit_code == 0, result.stderr
    child_pid = int(result.stdout.strip())
    assert child_pid != os.getpid()
    # The scope is derived from the RESOLVED cwd, never from model text.
    assert seen_roots == [str(tmp_path)]


def test_the_scope_is_derived_from_the_bounded_cwd(tmp_path):
    """A refused ``spec.cwd`` never reaches the scope factory at all."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen_roots: list[str | None] = []

    def _factory(active_root: str | None):
        seen_roots.append(active_root)

        @contextmanager
        def _cm() -> Iterator[Callable[[], None] | None]:
            yield None

        return _cm()

    _workspace_mod.register_shell_preexec_factory(_factory)

    spec = CommandVerification(argv=["/bin/true"], cwd=str(elsewhere))
    result = _run(spec, cwd=str(workspace))

    assert result.error is not None
    assert seen_roots == []


def test_a_raising_hook_becomes_a_failed_result_not_an_exception(tmp_path):
    """A ``preexec_fn`` that raises surfaces as ``SubprocessError`` — still total."""

    def _factory(active_root: str | None):
        @contextmanager
        def _cm() -> Iterator[Callable[[], None] | None]:
            def _hook() -> None:
                raise RuntimeError("no ruleset for you")

            yield _hook

        return _cm()

    _workspace_mod.register_shell_preexec_factory(_factory)

    result = _run(CommandVerification(argv=["/bin/true"]), cwd=str(tmp_path))

    assert result.error is not None
    assert result.exit_code == -1
    assert not isinstance(result.error, BaseException)


def test_an_unregistered_factory_still_spawns(tmp_path):
    """A core-only install (no ``mewbo_tools``) degrades to today's behaviour."""
    assert _workspace_mod._shell_preexec_factory is None

    result = _run(CommandVerification(argv=["/bin/echo", "ok"]), cwd=str(tmp_path))

    assert result.error is None
    assert result.stdout.strip() == "ok"


def test_subprocess_is_reachable_at_all(tmp_path):
    """Guards the suite itself: /bin/sh must exist for the refusal tests to mean anything."""
    assert subprocess.run(["/bin/true"], check=False).returncode == 0


# ---------------------------------------------------------------------------
# The REAL Landlock factory — enforcement, not wiring
# ---------------------------------------------------------------------------


class TestRealLandlockEnforcement:
    """What the kernel does to a verifier child, via the real ``mewbo_tools`` factory.

    Everything above proves the hook is COMPOSED. That is an argument, not a
    measurement, and a security control does not get to close on an argument.
    These register ``landlock.shell_preexec_for_root`` — the same callable
    ``_register_with_core()`` pushes down at import — and read the verdict off
    the child's own exit code and output.

    Every path is built under ``tmp_path``: ``tests/test_shell_landlock.py`` has
    been location-dependent before, and a scope compiled from a directory that
    happens to be the checkout would assert something about this machine.
    """

    def teardown_method(self):
        """Drop the config override — the preexec factory is the autouse fixture's job."""
        reset_config()

    @staticmethod
    def _bind(tmp_path):
        """Two real project checkouts, configured, with the real factory registered."""
        alpha = tmp_path / "alpha"
        alpha.mkdir()
        (alpha / "inside.txt").write_text("INSIDE_ALPHA\n", encoding="utf-8")
        beta = tmp_path / "beta"
        beta.mkdir()
        (beta / "secret.env").write_text("SECRET_BETA=value\n", encoding="utf-8")
        set_config_override(
            {
                "agent": {"shell_sandbox": True, "shell_denied_paths": []},
                "projects": {"alpha": {"path": str(alpha)}, "beta": {"path": str(beta)}},
            }
        )
        _workspace_mod.register_shell_preexec_factory(landlock.shell_preexec_for_root)
        return alpha, beta

    @requires_landlock
    def test_a_verifier_cannot_read_a_sibling_project(self, tmp_path):
        """The denial half — observed from the child, not from the parent's argv."""
        alpha, beta = self._bind(tmp_path)

        result = _run(
            CommandVerification(argv=["/bin/cat", str(beta / "secret.env")]),
            cwd=str(alpha),
        )

        assert result.exit_code != 0, f"the sibling was readable: {result.stdout!r}"
        assert "SECRET_BETA" not in result.stdout
        assert "SECRET_BETA" not in result.stderr

    @requires_landlock
    def test_a_verifier_can_still_read_its_own_workspace(self, tmp_path):
        """The positive half — without it, a denial passes when nothing ever ran.

        This is the vacuous-pass shape: a subprocess that fails to start also
        "cannot read the sibling". Asserting real bytes out of the workspace is
        what distinguishes confined from broken.
        """
        alpha, _beta = self._bind(tmp_path)

        result = _run(CommandVerification(argv=["/bin/cat", "inside.txt"]), cwd=str(alpha))

        assert result.error is None, result.error
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "INSIDE_ALPHA"

    def test_the_same_read_SUCCEEDS_with_no_factory_registered(self, tmp_path):
        """Proves the denial above discriminates rather than passing vacuously.

        Identical fixture, identical command — only the registration is dropped,
        which is both the pre-fix shape and a core-only install. The sibling's
        secret comes back verbatim, so the denial test is measuring the ruleset
        and not, say, a missing file or a broken ``cat``.
        """
        alpha, beta = self._bind(tmp_path)
        _workspace_mod.reset_shell_preexec_factory()

        result = _run(
            CommandVerification(argv=["/bin/cat", str(beta / "secret.env")]),
            cwd=str(alpha),
        )

        assert result.exit_code == 0, result.stderr
        assert "SECRET_BETA" in result.stdout

    def test_a_scratch_cwd_is_scoped_rather_than_falling_open(self, tmp_path):
        """A session with NO project bound still gets a deny-list, not ``None``.

        Measured, because the fall-open family is exactly what #431 was about.
        ``for_active_root`` returns ``None`` only when ``agent.shell_sandbox`` is
        off — an operator's explicit choice — never merely because the working
        directory is a session scratch dir rather than a configured project.
        Needs no kernel: this asserts the compiled scope, not its enforcement.
        """
        alpha, beta = self._bind(tmp_path)
        scratch = tmp_path / "session-scratch"
        scratch.mkdir()

        scope = ShellScope.for_active_root(str(scratch))

        assert scope is not None
        assert str(alpha) in scope.denied
        assert str(beta) in scope.denied
        assert str(scratch) in scope.allowed
