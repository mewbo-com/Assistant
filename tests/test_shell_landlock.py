"""Kernel-enforced filesystem scoping for shell subprocesses — a DENY-list.

The contracts, each of which was a real failure during the design rather than a
hypothetical:

1. **Nothing has to be enumerated to keep working.** The first design was an
   allowlist and broke the harness twice — a virtualenv's ``site-packages`` and
   ``/dev/null`` — both silently, because an unreadable directory surfaces as
   ``ModuleNotFoundError``, not as a denial. Inverting is the fix, so the runtime
   is asserted here with nothing granted for it.
2. **With no project bound, every configured project is denied.** This is the
   auto context, and the one that goes completely unscoped if the scope hangs
   off ``workspace_mode`` — a root agent is always ``full_access``.
3. **Opening a project reveals exactly that project**, and ``allowed_paths``
   re-permits a path that would otherwise be denied.
4. **The model cannot widen its own scope.** The scope comes from a
   loop-published contextvar, never from tool arguments — with no containment the
   loop honours a model-supplied ``root``, so deriving scope from it would let
   the model choose its own sandbox.
5. **A kernel without Landlock degrades**, logging once and completing the run.
6. **A grant that cannot be added narrows the scope, never removes it.** Any
   ``OSError`` from one path's open used to drop the whole ruleset and spawn the
   shell unconfined — a transient path error turning a sandbox into no sandbox,
   with one warning line as the only trace. A ruleset the kernel refuses
   outright, on a kernel that HAS Landlock, refuses the shell instead.

Enforcement is exercised by spawning REAL processes through the real
``ShellSession``: the claim is about what the kernel does to a child, and a
mocked spawn would prove nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.workspaces.workspace import active_project_root, get_active_project_root
from mewbo_tools.integration import landlock
from mewbo_tools.integration.landlock import LandlockAbi, ShellScope
from mewbo_tools.integration.shell_session import (
    ShellSession,
    ShellSessionStore,
    ShellStartArgs,
)

_HAS_LANDLOCK = sys.platform.startswith("linux") and LandlockAbi.probe().available
_NO_LANDLOCK_SKIP = "Landlock is a Linux-only kernel feature and is unavailable here"

requires_landlock = pytest.mark.skipif(not _HAS_LANDLOCK, reason=_NO_LANDLOCK_SKIP)


class _RecordingLogger:
    """Collects loguru calls so a test can assert one fired (caplog cannot)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def info(self, *_a: object, **_k: object) -> None:
        self.calls.append("info")

    def warning(self, *_a: object, **_k: object) -> None:
        self.calls.append("warning")

    def debug(self, *_a: object, **_k: object) -> None:
        self.calls.append("debug")


@pytest.fixture
def projects(tmp_path):
    """Three project checkouts and a shared data dir, all real on disk."""
    made = {}
    for name in ("alpha", "beta", "gamma"):
        d = tmp_path / name
        d.mkdir()
        (d / "secret.env").write_text(f"SECRET_{name.upper()}=value\n")
        made[name] = str(d)
    shared = tmp_path / "shared_data"
    shared.mkdir()
    (shared / "shared.txt").write_text("SHARED\n")
    made["shared"] = str(shared)
    return made


def _configure(projects_map: dict[str, dict], denied: list[str] | None = None) -> None:
    set_config_override(
        {
            "agent": {"shell_sandbox": True, "shell_denied_paths": denied or []},
            "projects": projects_map,
        }
    )


def _run(command: str, cwd: str, scope: ShellScope | None) -> dict[str, object]:
    """Run *command* to completion under *scope* and return its read payload."""
    session = ShellSession("t-1", ShellStartArgs(command=command, timeout=30.0), cwd, scope=scope)
    session.wait(30.0)
    return session.read()


class TestNothingIsEnumerated:
    """(1) The runtime keeps working with nothing granted for it."""

    @requires_landlock
    @pytest.mark.parametrize(
        "label,command",
        [
            ("stdlib", f'{sys.executable} -c "import json,ssl,sqlite3;print(\'OK\')"'),
            ("site_packages", f'{sys.executable} -c "import pydantic;print(\'OK\')"'),
            ("git", "git --version"),
            ("dev_null", "echo x > /dev/null && echo OK"),
            ("shell_pipe", "echo runtime | tr a-z A-Z"),
            ("scratch_dir", "mktemp -d -p . > /dev/null && echo OK"),
        ],
    )
    def test_runtime_survives_without_being_granted(self, projects, label, command):
        # Only the OTHER projects are denied; nothing about the interpreter,
        # system libraries or device nodes is named anywhere.
        scope = ShellScope(denied=(projects["beta"], projects["gamma"]))
        payload = _run(command, projects["alpha"], scope)
        assert payload["exit_code"] == 0, f"{label}: {payload['output']}"

    @requires_landlock
    def test_a_new_entry_cannot_be_made_in_an_EXPANDED_ancestor(self, projects, tmp_path):
        """The compile's one real limitation, pinned so it cannot surprise anyone.

        A denied path's parent is never granted wholesale — it is expanded into
        the children that exist AT BUILD TIME. So creating a new entry directly
        beside a denied sibling is refused, and a directory created there after
        the ruleset was built is invisible until the next spawn (the ruleset is
        rebuilt per spawn, so that window is one command).

        This does not bite the default deny set, which is configured projects and
        explicit extras — never a directory the agent creates into. It would bite
        a deployment that denied something directly under ``/tmp``.
        """
        scope = ShellScope(denied=(projects["beta"],))
        # tmp_path is the expanded ancestor here, since the denials live under it.
        blocked = _run(f"mkdir {tmp_path}/brand_new", projects["alpha"], scope)
        assert blocked["exit_code"] != 0
        # ...while creating inside the workspace itself is unaffected.
        allowed = _run("mkdir nested && echo OK", projects["alpha"], scope)
        assert allowed["exit_code"] == 0, allowed["output"]


class TestAutoContextDeniesEveryProject:
    """(2) With no project bound, nothing of anyone's is reachable."""

    def teardown_method(self):
        reset_config()

    def test_unbound_session_denies_all_configured_projects(self, projects, tmp_path):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        scope = ShellScope.for_active_root(str(scratch))
        assert scope is not None
        for name in ("alpha", "beta", "gamma"):
            assert projects[name] in scope.denied

    @requires_landlock
    def test_unbound_session_cannot_read_any_project(self, projects, tmp_path):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        scope = ShellScope.for_active_root(str(scratch))
        for name in ("alpha", "beta", "gamma"):
            payload = _run(f"cat {projects[name]}/secret.env", str(scratch), scope)
            assert payload["exit_code"] != 0, f"{name} was readable: {payload['output']}"
            assert "SECRET_" not in str(payload["output"])

    @requires_landlock
    def test_an_extra_denied_path_is_unreadable(self, projects, tmp_path):
        # This is how a deployment hides harness internals that sit outside every
        # configured project.
        harness = tmp_path / "harness"
        harness.mkdir()
        (harness / "app.json").write_text("API_KEY=leak\n")
        _configure({"alpha": {"path": projects["alpha"]}}, denied=[str(harness)])
        scope = ShellScope.for_active_root(projects["alpha"])
        payload = _run(f"cat {harness}/app.json", projects["alpha"], scope)
        assert payload["exit_code"] != 0
        assert "API_KEY" not in str(payload["output"])


class TestOpeningAProjectRevealsIt:
    """(3) The active project is reachable; its siblings are not."""

    def teardown_method(self):
        reset_config()

    def test_active_project_is_not_denied(self, projects):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scope = ShellScope.for_active_root(projects["alpha"])
        assert scope is not None
        assert projects["alpha"] not in scope.denied
        assert projects["beta"] in scope.denied

    @requires_landlock
    def test_active_project_readable_and_writable_siblings_are_not(self, projects):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scope = ShellScope.for_active_root(projects["alpha"])
        mine = _run(
            "cat secret.env && echo W > w.txt && cat w.txt", projects["alpha"], scope
        )
        assert mine["exit_code"] == 0, mine["output"]
        assert "SECRET_ALPHA" in str(mine["output"])

        theirs = _run(f"cat {projects['beta']}/secret.env", projects["alpha"], scope)
        assert theirs["exit_code"] != 0
        assert "SECRET_BETA" not in str(theirs["output"])

    @requires_landlock
    def test_allowed_paths_re_permits_a_denied_sibling(self, projects):
        # gamma is a configured project, so it would be denied — alpha's
        # allowed_paths subtracts it back out.
        _configure(
            {
                "alpha": {
                    "path": projects["alpha"],
                    "allowed_paths": [projects["shared"], projects["gamma"]],
                },
                "beta": {"path": projects["beta"]},
                "gamma": {"path": projects["gamma"]},
            }
        )
        scope = ShellScope.for_active_root(projects["alpha"])
        assert scope is not None
        assert projects["gamma"] not in scope.denied
        assert projects["beta"] in scope.denied

        ok = _run(f"cat {projects['shared']}/shared.txt", projects["alpha"], scope)
        assert ok["exit_code"] == 0 and "SHARED" in str(ok["output"])
        readmitted = _run(f"cat {projects['gamma']}/secret.env", projects["alpha"], scope)
        assert readmitted["exit_code"] == 0
        still_denied = _run(f"cat {projects['beta']}/secret.env", projects["alpha"], scope)
        assert still_denied["exit_code"] != 0


class TestAWorktreeInsideItsProject:
    """The default session shape: the workspace lives INSIDE a denied project.

    `create_session` provisions a git worktree under
    `<project>/.mewbo/worktrees/<name>`, and that worktree is not itself a
    configured project — so the project IS denied while the session's own
    working directory sits beneath it. Expansion alone leaves the session unable
    to read the directory it works in, which is a total blocker on the default
    path. Measured against the real deployment layout before it was fixed.
    """

    def teardown_method(self):
        reset_config()

    @pytest.fixture
    def worktree(self, projects):
        wt = os.path.join(projects["alpha"], ".mewbo", "worktrees", "task-1")
        os.makedirs(wt)
        with open(os.path.join(wt, "mine.txt"), "w") as fh:
            fh.write("WORKTREE_OK\n")
        return wt

    def test_the_worktree_is_granted_even_though_its_project_is_denied(
        self, projects, worktree
    ):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scope = ShellScope.for_active_root(worktree)
        assert scope is not None
        # The project is still denied — only the worktree is carved out.
        assert projects["alpha"] in scope.denied
        assert worktree in scope.grants()

    @requires_landlock
    def test_the_session_can_work_in_its_worktree_and_not_in_its_siblings(
        self, projects, worktree
    ):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scope = ShellScope.for_active_root(worktree)
        mine = _run("cat mine.txt && echo W > w.txt && cat w.txt", worktree, scope)
        assert mine["exit_code"] == 0, mine["output"]
        assert "WORKTREE_OK" in str(mine["output"])
        # The rest of the parent project stays denied.
        parent = _run(f"cat {projects['alpha']}/secret.env", worktree, scope)
        assert parent["exit_code"] != 0
        assert "SECRET_ALPHA" not in str(parent["output"])
        sibling = _run(f"cat {projects['beta']}/secret.env", worktree, scope)
        assert sibling["exit_code"] != 0


class TestTheHarnessHidesItself:
    """Mewbo's own source and config are denied by DEFAULT, not by configuration.

    Leaving this to `shell_denied_paths` shipped a default where the config
    holding the API keys stayed readable — measured on the deployment after
    every other surface had been scoped.
    """

    @pytest.fixture(autouse=True)
    def _unpin_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolve the config where a real deployment has it: inside the repo.

        The suite pins ``MEWBO_CONFIG_DIR`` at a throwaway directory (root
        ``conftest.py``) so subprocess probes never read the developer's own
        config. That directory is OUTSIDE the checkout, so it is a genuine
        extra harness root — which is the opposite of what these tests assert,
        and true only of the pin rather than of the harness.
        """
        monkeypatch.delenv("MEWBO_CONFIG_DIR", raising=False)

    def teardown_method(self):
        reset_config()

    def test_harness_source_and_config_are_derived(self, projects):
        _configure({"alpha": {"path": projects["alpha"]}})
        roots = ShellScope.harness_roots(projects["alpha"])
        # On a source checkout the packages tree is derivable; on a pip install
        # it sits under sys.prefix and _survivable drops it. Either is correct,
        # so assert the PROPERTY: nothing returned may contain the runtime.
        runtime = os.path.realpath(sys.prefix)
        for root in roots:
            assert not (runtime == root or runtime.startswith(root + os.sep)), (
                f"{root} contains the runtime and would stop Python starting"
            )

    def test_working_ON_mewbo_is_an_opt_in_and_never_a_derivation(self):
        # The dev case. It used to be derived — a harness root inside the active
        # root was dropped — and that erased the WHOLE self-deny on the shape
        # that matters, where the checkout IS the active root. It is now
        # ``agent.harness_self_deny``, and the active root changes nothing.
        # Full coverage of the switch is in ``test_harness_self_deny.py``.
        import mewbo_core

        pkg = os.path.dirname(os.path.abspath(mewbo_core.__file__))
        parts = pkg.split(os.sep)
        if "packages" not in parts:
            pytest.skip("not a source checkout, so there is no packages tree to reach")
        repo_root = os.path.realpath(os.sep.join(parts[: parts.index("packages")]))
        assert ShellScope.harness_roots(repo_root) == ShellScope.harness_roots(None)
        set_config_override({"agent": {"harness_self_deny": False}})
        assert ShellScope.harness_roots(repo_root) == set()

    def test_the_derived_roots_survive_the_runtime_guard(self, projects):
        _configure({"alpha": {"path": projects["alpha"]}})
        scope = ShellScope.for_active_root(projects["alpha"])
        assert scope is not None
        # Whatever survived is a real directory and never the runtime's.
        for root in scope.denied:
            assert os.path.isdir(root) or root not in ShellScope.harness_roots(None)


class TestTheModelCannotWidenItsScope:
    """(4) Scope comes from the loop, never from tool arguments."""

    def teardown_method(self):
        reset_config()

    def test_scope_is_read_from_the_contextvar_not_the_cwd(self, projects):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        # The loop publishes alpha. A model-supplied cwd/root naming beta must
        # not make beta the active project.
        with active_project_root(projects["alpha"]):
            assert get_active_project_root() == projects["alpha"]
            scope = ShellScope.for_active_root(get_active_project_root())
        assert scope is not None
        assert projects["beta"] in scope.denied
        assert projects["alpha"] not in scope.denied

    @requires_landlock
    def test_store_ignores_a_model_supplied_cwd_when_scoping(self, projects):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        store = ShellSessionStore()
        try:
            # cwd says beta; the loop says alpha. beta must stay unreadable.
            with active_project_root(projects["alpha"]):
                session = store.create(
                    ShellStartArgs(
                        command=f"cat {projects['beta']}/secret.env", timeout=30.0
                    ),
                    projects["alpha"],
                )
            session.wait(30.0)
            assert "SECRET_BETA" not in str(session.read()["output"])
        finally:
            store.shutdown()

    def test_no_published_root_still_denies_every_project(self, projects):
        _configure({k: {"path": v} for k, v in projects.items() if k != "shared"})
        scope = ShellScope.for_active_root(None)
        assert scope is not None
        for name in ("alpha", "beta", "gamma"):
            assert projects[name] in scope.denied


class TestDegradesAndDisables:
    """(5) A missing sandbox must never fail a run, and the flag must be honoured."""

    def teardown_method(self):
        reset_config()

    def test_flag_off_produces_no_scope(self, projects):
        set_config_override(
            {
                "agent": {"shell_sandbox": False},
                "projects": {"alpha": {"path": projects["alpha"]}},
            }
        )
        assert ShellScope.for_active_root(projects["beta"]) is None

    def test_only_the_harness_remains_when_the_active_project_is_the_only_one(
        self, projects, monkeypatch
    ):
        # A single configured project that IS the active one contributes no
        # project denials — but the harness denies itself unconditionally, so
        # the scope is not empty. Pinning derivation to nothing would let the
        # harness-hiding default be deleted without a test noticing.
        fake = staticmethod(lambda _a: {"/opt/fake-harness"})
        monkeypatch.setattr(ShellScope, "harness_roots", fake)
        _configure({"alpha": {"path": projects["alpha"]}})
        scope = ShellScope.for_active_root(projects["alpha"])
        assert scope is not None
        assert scope.denied == ("/opt/fake-harness",)

    def test_nothing_to_deny_at_all_produces_no_scope(self, projects, monkeypatch):
        # With no harness roots either, an empty ruleset is just cost.
        monkeypatch.setattr(ShellScope, "harness_roots", staticmethod(lambda _a: set()))
        _configure({"alpha": {"path": projects["alpha"]}})
        assert ShellScope.for_active_root(projects["alpha"]) is None

    def test_a_deny_set_emptied_by_the_runtime_guard_produces_no_scope(
        self, projects, monkeypatch
    ):
        # Ordering matters: filtering has to happen BEFORE the empty check, or a
        # fully-filtered deny set still builds a ruleset that denies nothing and
        # costs a syscall per directory on "/".
        monkeypatch.setattr(landlock, "logger", _RecordingLogger())
        monkeypatch.setattr(ShellScope, "harness_roots", staticmethod(lambda _a: set()))
        _configure({"alpha": {"path": projects["alpha"]}}, denied=[os.path.realpath(sys.prefix)])
        assert ShellScope.for_active_root(projects["alpha"]) is None

    def test_unavailable_kernel_logs_once_and_scopes_nothing(self, projects, monkeypatch):
        recorder = _RecordingLogger()
        monkeypatch.setattr(landlock, "logger", recorder)
        monkeypatch.setattr(landlock, "_syscall", lambda *_a: -1)
        LandlockAbi.probe.cache_clear()
        try:
            assert LandlockAbi.probe().available is False
            assert recorder.calls == ["info"], "an unusable kernel is reported exactly once"
            scope = ShellScope(denied=(projects["beta"],))
            with scope.enforced() as hook:
                assert hook is None, "nothing to apply means no preexec_fn at all"
            payload = _run(f"cat {projects['beta']}/secret.env", projects["alpha"], scope)
            assert payload["exit_code"] == 0, "the run must still complete"
        finally:
            LandlockAbi.probe.cache_clear()

    @requires_landlock
    def test_a_child_that_cannot_apply_the_ruleset_never_runs(
        self, projects, tmp_path, monkeypatch
    ):
        # Parent-side setup failures degrade on purpose. Once the ruleset EXISTS,
        # a child that cannot apply it must die rather than run unscoped.
        marker = tmp_path / "SHOULD_NOT_EXIST"
        real = landlock._child_syscall

        def failing(*args):
            if args and getattr(args[0], "value", None) == landlock._SYS_RESTRICT_SELF:
                return -1
            return real(*args)

        monkeypatch.setattr(landlock, "_child_syscall", failing)
        scope = ShellScope(denied=(projects["beta"],))
        with scope.enforced() as hook:
            assert hook is not None, "the ruleset itself must have been built"
            proc = subprocess.Popen(
                f"touch {marker}", shell=True, preexec_fn=hook,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 127
        assert not marker.exists(), "FAIL-OPEN: the command ran without a ruleset"
        assert b"refusing to run unscoped" in out


class TestAGrantThatCannotBeAddedNarrowsTheScope:
    """(6) A rule that will not go in must never cost the whole sandbox.

    The compile opens every granted path, and an open can fail for reasons that
    have nothing to do with the agent: a directory removed by another process, a
    mount going away, a permissions flap, an fd limit. Reading any of those as
    "run unconfined" inverts the control exactly when it is least expected —
    measured, with one warning line as the only trace on a run that otherwise
    looked successful.
    """

    @requires_landlock
    def test_a_granted_path_that_vanishes_still_confines_the_shell(
        self, projects, tmp_path, monkeypatch
    ):
        """The reproduction, in its original shape: a grant lost to a race.

        ``grants()`` filters on ``os.path.isdir`` and the ruleset opens the
        survivors, so a path that exists at the filter and is gone at the open is
        reachable by construction. What must NOT follow is an unscoped shell.
        """
        vanishing = tmp_path / "vanishes_between_the_filter_and_the_open"
        vanishing.mkdir()
        real_grants = ShellScope.grants

        def grants_then_vanish(self) -> list[str]:
            compiled = real_grants(self)
            shutil.rmtree(vanishing)  # the window, forced open deterministically
            return compiled

        monkeypatch.setattr(ShellScope, "grants", grants_then_vanish)
        scope = ShellScope(denied=(projects["beta"],), allowed=(str(vanishing),))
        payload = _run(f"cat {projects['beta']}/secret.env", projects["alpha"], scope)
        assert payload["exit_code"] != 0, "FAIL-OPEN: one lost grant unscoped the shell"
        assert "SECRET_BETA" not in str(payload["output"])

    @requires_landlock
    def test_the_lost_grant_is_named_to_the_caller_and_not_only_to_a_log(
        self, projects, monkeypatch
    ):
        """A narrower scope than was asked for is a fact the caller has to see.

        A log sink is not a return channel: the process that would act on the
        difference is the one that never learns of it.
        """
        recorder = _RecordingLogger()
        monkeypatch.setattr(landlock, "logger", recorder)
        doomed = projects["gamma"]
        real_add = ShellScope._add_rule

        def refuse_one(fd: int, path: str, access: int) -> None:
            if path == doomed:
                raise OSError(2, "No such file or directory")
            real_add(fd, path, access)

        monkeypatch.setattr(ShellScope, "_add_rule", staticmethod(refuse_one))
        scope = ShellScope(denied=(projects["beta"],))
        build = scope._create_ruleset()
        try:
            assert build.fd is not None, "the ruleset must survive one refused rule"
            assert build.ungranted == (doomed,), "the caller reads the loss by name"
            warnings = [c for c in recorder.calls if c == "warning"]
            assert warnings == ["warning"], "summarised once, not once per lost path"
        finally:
            if build.fd is not None:
                os.close(build.fd)

    def test_a_ruleset_the_kernel_refuses_outright_fails_the_shell(self, projects, monkeypatch):
        """Landlock is present and still produced nothing — so refuse, do not spawn.

        This is the repo's own law in the direction that matters: a filter that
        cannot be applied refuses rather than falling back to everything. It is
        NOT the absent-kernel path, which stays a documented degradation.
        """
        monkeypatch.setattr(LandlockAbi, "probe", staticmethod(lambda: LandlockAbi(version=5)))
        monkeypatch.setattr(landlock, "_syscall", lambda *_a: -1)
        scope = ShellScope(denied=(projects["beta"],))
        with pytest.raises(landlock.SandboxUnavailableError):
            with scope.enforced():
                pytest.fail("a shell must never be spawned from a refused ruleset")

    def test_the_refusal_reaches_the_model_as_an_error_not_a_traceback(
        self, projects, monkeypatch
    ):
        """Refusing is only useful if the refusal is legible where it lands.

        ``ShellSession`` turns a failed spawn into a ``_start_error`` the model
        reads back; an exception outside that envelope escapes as a traceback
        from a constructor, which is why the refusal is an ``OSError``.
        """
        monkeypatch.setattr(LandlockAbi, "probe", staticmethod(lambda: LandlockAbi(version=5)))
        monkeypatch.setattr(landlock, "_syscall", lambda *_a: -1)
        scope = ShellScope(denied=(projects["beta"],))
        payload = _run(f"cat {projects['beta']}/secret.env", projects["alpha"], scope)
        assert payload["exit_code"] != 0
        assert "SECRET_BETA" not in str(payload["output"])
        assert "refusing to continue unscoped" in str(payload["output"])

    def test_a_server_launch_still_decides_for_itself(self, projects, monkeypatch):
        """The self-application half reports the same refusal without raising.

        A model-authored shell command has no claim to run, but a language or MCP
        server that fails to start takes tool discovery or editor diagnostics down
        wholesale — so that caller is handed the fact and chooses.
        """
        monkeypatch.setattr(LandlockAbi, "probe", staticmethod(lambda: LandlockAbi(version=5)))
        monkeypatch.setattr(landlock, "_syscall", lambda *_a: -1)
        assert ShellScope(denied=(projects["beta"],)).apply_to_self() is False


class TestCompilation:
    """The deny→allow compile, which is what makes Landlock express a denial."""

    def test_grants_exclude_the_denied_path_but_cover_its_siblings(self, projects):
        scope = ShellScope(denied=(projects["beta"],))
        grants = scope.grants()
        assert projects["beta"] not in grants
        assert projects["alpha"] in grants, "a sibling of a denial stays reachable"
        assert "/usr" in grants, "the runtime is reached by expansion, not by a list"

    def test_the_filesystem_root_is_never_a_grant(self, projects):
        # A rule beneath "/" would grant everything. Expansion means it can never
        # appear, which is why the deny-list has no fail-open equivalent.
        assert os.sep not in ShellScope(denied=(projects["beta"],)).grants()

    def test_denying_the_root_is_refused(self, projects, monkeypatch):
        _configure({"alpha": {"path": projects["alpha"]}}, denied=["/"])
        try:
            scope = ShellScope.for_active_root(projects["alpha"])
            assert scope is None or os.sep not in scope.denied
        finally:
            reset_config()

    def test_a_denial_containing_the_runtime_is_refused(self, projects, monkeypatch):
        """Measured: denying a deployment's `/app` stops Python starting at all.

        The harness source and its virtualenv often share a root, so the denial
        an operator means ("hide my source") and the one they write ("deny
        /app") are not the same thing. Refusing loudly beats a sandbox that is
        indistinguishable from a broken interpreter.
        """
        recorder = _RecordingLogger()
        monkeypatch.setattr(landlock, "logger", recorder)
        runtime_root = os.path.realpath(sys.prefix)
        assert ShellScope._survivable({runtime_root}) == set()
        assert ShellScope._survivable({os.path.dirname(runtime_root)}) == set()
        assert recorder.calls == ["warning", "warning"]
        # A path that does NOT contain the runtime survives untouched.
        assert ShellScope._survivable({projects["beta"]}) == {projects["beta"]}

    def test_device_creation_is_never_granted(self):
        abi = LandlockAbi.probe()
        if not abi.available:
            pytest.skip(_NO_LANDLOCK_SKIP)
        for name in ("MAKE_CHAR", "MAKE_BLOCK"):
            assert not abi.grant_access() & landlock._FS_BITS[name][0]
        assert abi.grant_access() & landlock._FS_BITS["WRITE_FILE"][0]
