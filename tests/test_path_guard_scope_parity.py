#!/usr/bin/env python3
"""The two halves of the filesystem boundary must agree.

``ShellScope``/Landlock confines the shell's subprocesses at the kernel;
``resolve_safe_path`` validates a path ARGUMENT for every other tool. They are
two enforcement points for ONE policy, and three ways they disagreed are pinned
here — each named by its mechanism, because each fails silently rather than
loudly.

1. **The denial set was derived twice.** ``ShellScope.for_active_root`` read
   ``agent.shell_denied_paths``; the path guard never did. An operator who
   denied a directory got it denied to the shell and ALLOWED to ``read_file``
   — the same escape the sandbox exists to close, reached by not being a shell.
   The cure is ONE derivation (``ShellScope.denied_for``) applied as a
   POST-condition at ``resolve_safe_path``'s return, so BOTH of its mutually
   exclusive exits — the containment branch and the tenant union — pass through
   it.
2. **The no-published-root case inverted.** ``ShellScope.for_active_root(None)``
   falls CLOSED; the guard read "no root published" as "not scoped" and fell
   OPEN, which additionally re-admitted whatever ``root`` ARGUMENT the model
   named. The gate is now the FLAG.
3. **The active-root contextvar was published on ONE dispatch branch.** It
   wrapped only the registry ``else:``, so a session tool, a skill activation
   and a spawn each ran with ``get_active_project_root() is None`` and inherited
   (2). Observed from INSIDE a session tool's ``handle()``, because an
   after-the-fact assertion cannot tell "published around the call" from "never
   published at all".

Fixtures build under ``tmp_path`` and derive the harness roots from
``ShellScope.harness_roots`` rather than naming a directory, because that answer
differs per deployment and per checkout location.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.common import MockSpeaker
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.hooks import HookManager
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT, SESSION_TEMP_ROOT
from mewbo_core.tooling.tool_registry import ToolRegistry
from mewbo_core.workspaces.workspace import (
    WorkspaceContainment,
    active_containment,
    active_project_root,
    get_active_project_root,
)
from mewbo_tools.core import resolve_safe_path
from mewbo_tools.integration.landlock import ShellScope


@pytest.fixture
def denied_tree(tmp_path, app_config_file):
    """One configured project holding a directory the operator denies.

    The denial lives INSIDE the active project on purpose: that is the only
    shape ``_get_allowed_roots``'s scoped list cannot already refuse, so a green
    result here can only come from the denial set and not from the scope.
    """
    proj = tmp_path / "proj"
    secrets = proj / "secrets"
    sub = proj / "sub"
    shared = tmp_path / "shared"
    for d in (secrets, sub, shared):
        d.mkdir(parents=True)
    (proj / "ok.py").write_text("ok = True\n")
    (secrets / "id_rsa").write_text("PRIVATE\n")
    (shared / "notes.txt").write_text("SHARED\n")
    set_config_override(
        {
            "projects": {"proj": {"path": str(proj), "allowed_paths": [str(shared)]}},
            "agent": {"shell_denied_paths": [str(secrets)]},
        }
    )
    return {
        "proj": str(proj),
        "ok": str(proj / "ok.py"),
        "sub": str(sub),
        "secrets": str(secrets),
        "secret_file": str(secrets / "id_rsa"),
        "shared": str(shared),
        "shared_file": str(shared / "notes.txt"),
    }


class TestDeniedSetParity:
    """(1) ``agent.shell_denied_paths`` binds BOTH halves, from ONE derivation.

    Measured with NO active root published, which is both the shape every
    ``/v1/structured`` run has and the only one where the operator's declaration
    is doing the work on its own: with a root published, the scoped root list
    already refuses anything outside the session's project, so a green result
    there could not tell the two mechanisms apart.
    """

    def teardown_method(self):
        reset_config()

    def test_the_shell_scope_denies_the_operator_declared_directory(self, denied_tree):
        # The control: the half that always honoured it. Without this the guard
        # assertions below could pass against a path nothing ever denied.
        scope = ShellScope.for_active_root(None)
        assert scope is not None
        assert denied_tree["secrets"] in scope.denied

    def test_both_halves_read_the_same_derivation(self, denied_tree):
        # DRY asserted, not assumed: ``for_active_root`` is now a gate plus a
        # call to ``denied_for``, which is what the path guard consults too.
        denied, allowed = ShellScope.denied_for(denied_tree["proj"])
        scope = ShellScope.for_active_root(denied_tree["proj"])
        assert scope is not None
        assert (scope.denied, scope.allowed) == (denied, allowed)

    @pytest.mark.parametrize("write", [False, True])
    def test_the_path_guard_refuses_it_through_the_tenant_union_exit(
        self, denied_tree, write
    ):
        with pytest.raises(ValueError) as exc:
            resolve_safe_path(denied_tree["secret_file"], write=write)
        assert denied_tree["secrets"] in str(exc.value)
        assert "Allowed roots:" in str(exc.value), "a denial must name what IS allowed"

    def test_the_containing_project_is_still_reachable_on_that_exit(self, denied_tree):
        # The control that keeps the refusal above honest: the union still
        # reaches the project, so the denial subtracted a directory rather than
        # collapsing the scope.
        assert str(resolve_safe_path(denied_tree["ok"])) == denied_tree["ok"]

    @pytest.mark.parametrize("write", [False, True])
    def test_the_path_guard_refuses_it_through_the_containment_exit(
        self, denied_tree, write
    ):
        # The exit a contained sub-agent takes. Subtracting the denial inside
        # ``_get_allowed_roots`` would be invisible here, which is why the check
        # is a post-condition at the return both exits share.
        contained = WorkspaceContainment(mode="workspace_write", root=denied_tree["proj"])
        assert contained.active
        assert contained.permits_read(denied_tree["secret_file"]) is True
        with active_containment(contained):
            with pytest.raises(ValueError):
                resolve_safe_path(denied_tree["secret_file"], write=write)
            # Non-regression on the same exit: the rest of the workspace stays.
            assert str(resolve_safe_path(denied_tree["ok"])) == denied_tree["ok"]

    def test_a_denial_inside_the_ACTIVE_project_is_void_in_BOTH_halves(self, denied_tree):
        # Stated because it is surprising, and pinned because the two halves
        # agree on it: ``readmitted_for`` puts the active root in ``allowed``,
        # and Landlock grants beneath an allowed path unconditionally — so a
        # deny-list entry under the session's own project does not survive
        # either mechanism. An operator wanting it hidden must not bind the
        # project that contains it.
        scope = ShellScope.for_active_root(denied_tree["proj"])
        assert scope is not None
        assert denied_tree["secrets"] in scope.denied
        assert denied_tree["proj"] in scope.grants(), "the grant re-admits the denial"
        with active_project_root(denied_tree["proj"]):
            assert (
                str(resolve_safe_path(denied_tree["secret_file"]))
                == denied_tree["secret_file"]
            )


class TestReadmissionStillWins:
    """A project's ``allowed_paths`` outrank a denial, as Landlock's grants do."""

    def teardown_method(self):
        reset_config()

    def test_an_allowed_path_resolves_even_though_it_is_denied(self, tmp_path, app_config_file):
        proj = tmp_path / "proj"
        shared = tmp_path / "shared"
        for d in (proj, shared):
            d.mkdir()
        (shared / "notes.txt").write_text("SHARED\n")
        # ``shared`` is BOTH re-admitted by the active project and named in the
        # operator's deny list — the precedence question, made explicit.
        set_config_override(
            {
                "projects": {"proj": {"path": str(proj), "allowed_paths": [str(shared)]}},
                "agent": {"shell_denied_paths": [str(shared)]},
            }
        )
        target = str(shared / "notes.txt")
        assert str(shared) in ShellScope.readmitted_for(str(proj))
        with active_project_root(str(proj)):
            assert str(resolve_safe_path(target)) == target


class TestNoPublishedRootDoesNotFallOpen:
    """(2) With scoping on and nothing published, the harness stays out of reach."""

    def teardown_method(self):
        reset_config()

    @staticmethod
    def _harness_root_under_cwd() -> str:
        cwd = Path(os.getcwd()).resolve()
        under = sorted(
            r for r in ShellScope.harness_roots(None) if Path(r).is_relative_to(cwd)
        )
        assert under, "no harness root under the process CWD — nothing to measure"
        return under[0]

    def test_a_harness_root_is_refused(self, app_config_file):
        # ``harness_roots(None)`` names Mewbo's own source trees and the
        # directory holding ``app.json``. The tenant union reaches them because
        # the process CWD is the harness on a container deployment, which is
        # exactly how ``read_file`` returned the file holding the API keys.
        target = os.path.join(self._harness_root_under_cwd(), "probe.txt")
        with pytest.raises(ValueError) as exc:
            resolve_safe_path(target)
        assert "Allowed roots:" in str(exc.value)

    def test_the_cwd_itself_is_still_reachable(self, app_config_file):
        # The control for the test above: the union is NOT collapsed wholesale,
        # so the refusal measures the denial and not a path that was already out.
        target = os.path.join(os.getcwd(), "pyproject.toml")
        assert str(resolve_safe_path(target)) == str(Path(target).resolve())

    def test_a_sibling_configured_project_is_still_reachable(self, tmp_path, app_config_file):
        # The residual, pinned so it is a decision and not a surprise: falling
        # fully closed here would break every direct library caller, test and
        # CLI invocation, none of which publish a root. Landlock DOES deny this.
        other = tmp_path / "other"
        other.mkdir()
        (other / "f.txt").write_text("x\n")
        set_config_override({"projects": {"other": {"path": str(other)}}})
        assert str(resolve_safe_path(str(other / "f.txt"))) == str(other / "f.txt")
        assert str(other) in ShellScope.denied_for(None)[0]


class TestTheRootArgumentMayNarrowNeverWiden:
    """(2, cont.) The gate is the FLAG, not whether a root happened to be published."""

    def teardown_method(self):
        reset_config()

    def test_an_out_of_scope_root_is_refused_with_nothing_published(self, denied_tree, tmp_path):
        # ``read_file(path="id_rsa", root="/home/…/.ssh")`` — the model naming
        # its own sandbox. Every ``/v1/structured`` run reaches the loop with no
        # cwd, so this is the live shape, not a hypothetical one.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("PRIVATE\n")
        with pytest.raises(ValueError):
            resolve_safe_path("id_rsa", root=str(outside))

    def test_a_narrowing_root_still_resolves_with_nothing_published(self, denied_tree):
        # The non-regression half: a subdirectory of an allowed root narrows,
        # and a relative path still resolves against the root it was given.
        got = resolve_safe_path("../ok.py", root=denied_tree["sub"])
        assert str(got) == denied_tree["ok"]

    def test_the_cwd_fallback_every_path_tool_uses_still_resolves(self, denied_tree):
        # EVERY model-facing call site spells the root the same way —
        # ``argument.get("root") or os.getcwd()`` (`aider_file_tools.py:47,85`,
        # `aider_edit_blocks.py:105`, `file_edit_tool.py:97`,
        # `aider_shell_tool.py:36`). So the fallback is the process CWD, which
        # the union appends in BOTH shapes; measured here rather than reasoned
        # about, because it is the one root every path tool passes on the calls
        # where the model named none.
        target = os.path.join(os.getcwd(), "pyproject.toml")
        got = resolve_safe_path("pyproject.toml", root=os.getcwd())
        assert str(got) == str(Path(target).resolve())

    def test_a_session_temp_dir_root_still_resolves(self, denied_tree):
        # The other root a live unscoped session passes. Both Mewbo scratch
        # roots sit under ``/tmp/mewbo``, which ``_get_allowed_roots`` appends
        # unconditionally in both shapes, so a session-temp root NARROWS and is
        # unaffected by the new gate. This was the one plausible way fix (2)
        # could have broken every unscoped session.
        assert SESSION_TEMP_ROOT.startswith("/tmp/mewbo")
        assert PLAN_DIR_ROOT.startswith("/tmp/mewbo")
        session_dir = os.path.join(SESSION_TEMP_ROOT, "s-parity")
        got = resolve_safe_path("scratch.txt", root=session_dir)
        assert str(got) == os.path.join(session_dir, "scratch.txt")

    def test_the_flag_off_path_is_unchanged(self, denied_tree, tmp_path):
        # Byte-identical where scoping does not apply: the advisory root is
        # honoured exactly as before, and the denial set is not consulted.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("PRIVATE\n")
        set_config_override({"agent": {"path_scope_to_active_project": False}})
        assert str(resolve_safe_path("id_rsa", root=str(outside))) == str(
            outside / "id_rsa"
        )
        assert str(resolve_safe_path(denied_tree["secret_file"])) == denied_tree[
            "secret_file"
        ]


class _RootObservingSessionTool:
    """A ``SessionTool`` that records the active project root DURING its run.

    Standalone rather than a subclass: ``SessionTool`` is a structural Protocol,
    which is the shape every plugin tool has. Observing from inside ``handle()``
    is the point — a contextvar read after ``_execute_tool_call`` returns cannot
    distinguish "published around the call" from "never published".
    """

    modes = frozenset({"act"})

    def __init__(self) -> None:
        self.tool_id = "observe_root"
        self.schema = {
            "name": self.tool_id,
            "description": "records the active project root",
            "parameters": {"type": "object", "properties": {}},
        }
        self.seen_root: str | None = None
        self.called = False

    async def handle(self, action_step):
        self.called = True
        self.seen_root = get_active_project_root()
        return MockSpeaker(content="ok")

    def should_terminate_run(self) -> bool:
        return False


class TestTheContextvarReachesEveryDispatchBranch:
    """(3) The scope must not depend on which dispatch arm a tool lives on."""

    def teardown_method(self):
        reset_config()

    @staticmethod
    def _loop(cwd: str, tool: _RootObservingSessionTool) -> ToolUseLoop:
        policy = MagicMock(spec=PermissionPolicy)
        policy.decide.return_value = PermissionDecision.ALLOW
        hooks = MagicMock(spec=HookManager)
        hooks.run_pre_tool_use.side_effect = lambda step: step
        hooks.run_post_tool_use.side_effect = lambda step, result: result
        hooks.run_permission_request.side_effect = lambda step, decision: decision
        ctx = AgentContext.root(
            model_name="primary-model",
            max_depth=5,
            registry=AgentHypervisor(max_concurrent=100),
        )
        with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = MagicMock()
            return ToolUseLoop(
                agent_context=ctx,
                tool_registry=ToolRegistry(),
                permission_policy=policy,
                hook_manager=hooks,
                session_id="s-scope",
                cwd=cwd,
                extra_session_tools=[tool],
            )

    def test_a_session_tool_sees_the_loops_cwd(self, tmp_path, app_config_file):
        proj = tmp_path / "proj"
        proj.mkdir()
        tool = _RootObservingSessionTool()
        loop = self._loop(str(proj), tool)
        asyncio.run(
            loop._execute_tool_call(
                {"id": "c1", "name": "observe_root", "args": {}}, []
            )
        )
        assert tool.called, "the fake session tool never ran"
        assert tool.seen_root == str(proj)

    def test_the_registry_branch_still_sees_it(self, tmp_path, app_config_file):
        # The branch that always published it, kept as the control so a hoist
        # that broke the original arm cannot pass this class.
        proj = tmp_path / "proj"
        proj.mkdir()
        seen: list[str | None] = []

        tool = MagicMock()
        tool.arun = AsyncMock(
            side_effect=lambda step: (
                seen.append(get_active_project_root()),
                MockSpeaker(content="ok"),
            )[1]
        )
        registry = MagicMock(spec=ToolRegistry)
        registry.get.return_value = tool
        registry.get_spec.return_value = None

        observer = _RootObservingSessionTool()
        loop = self._loop(str(proj), observer)
        loop._tool_registry = registry
        asyncio.run(
            loop._execute_tool_call({"id": "c2", "name": "some_tool", "args": {}}, [])
        )
        assert seen == [str(proj)]
