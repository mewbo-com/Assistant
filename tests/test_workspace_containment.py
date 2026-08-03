#!/usr/bin/env python3
"""Workspace containment — filesystem firebreak.

The nine-point contract for ``workspace_mode`` v1, plus the escape regression
that motivated flipping ``agent.workspace_enforcement`` on by default:

1. workspace_write child cannot READ outside its root (sibling / /app / cred).
2. ...cannot WRITE outside its root.
3. read_only: writes denied everywhere; reads confined.
4. flag OFF (opt-out): every path byte-identical to the pre-existing tenant
   union — this is what the escape looked like before the default flipped,
   and it stays reachable for an operator who explicitly disables enforcement.
5. deep-nesting monotonicity via ``child()``.
6. authoritative root injection: a model-supplied wider root is IGNORED under
   active containment, HONORED (advisory) under full_access / flag off.
7. capability_mode × workspace_mode narrow INDEPENDENTLY (shared ``_narrow``).
8. ``WorkspaceContainment`` unit table (realpath prefix, symlink escape,
   /tmp/mewbo carve-out, PLAN_DIR).
9. external-cwd root derivation — the anchored cwd becomes the containment root.

Enforcement is exercised by driving ``resolve_safe_path`` directly with an
explicit containment (and via the loop's active-containment context), never by
standing up a real LLM turn. ``TestTenantUnionEscapeAtShippedDefaults`` below
is the one exception that matters: it reproduces the actual cross-project
escape against the config the ``app_config_file`` fixture WRITES (the true
shipped default, not a forced override), because a test that only ever forces
the flag proves the mechanism works and nothing about what ships.
"""

from __future__ import annotations

import os

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.config import AppConfig, reset_config, set_config_override
from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT
from mewbo_core.workspaces.workspace import (
    WORKSPACE_MODE_RANK,
    WorkspaceContainment,
    active_containment,
    get_active_containment,
)
from mewbo_tools.core import resolve_safe_path


@pytest.fixture
def workspace(tmp_path):
    """A workspace root + a sibling dir OUTSIDE it, both real on disk."""
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.py").write_text("x = 1\n")
    sib = tmp_path / "sibling"
    sib.mkdir()
    (sib / "secret.txt").write_text("shh\n")
    return {"ws": str(ws), "sibling": str(sib)}


# ---------------------------------------------------------------------------
# (8) WorkspaceContainment unit table
# ---------------------------------------------------------------------------


class TestWorkspaceContainmentModel:
    def test_active_only_when_not_full_access(self):
        assert WorkspaceContainment(mode="full_access", root="/ws").active is False
        assert WorkspaceContainment(mode="workspace_write", root="/ws").active is True
        assert WorkspaceContainment(mode="read_only", root="/ws").active is True

    def test_allowed_roots_carve_out_scratch_and_plan(self, workspace):
        roots = WorkspaceContainment(
            mode="workspace_write", root=workspace["ws"]
        ).allowed_roots()
        assert workspace["ws"] in roots
        assert "/tmp/mewbo" in roots
        assert PLAN_DIR_ROOT in roots

    def test_empty_root_confines_to_scratch_only(self):
        roots = WorkspaceContainment(mode="read_only", root="").allowed_roots()
        assert roots == ("/tmp/mewbo", PLAN_DIR_ROOT)

    def test_realpath_prefix_inside_and_outside(self, workspace):
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        assert c.permits_read(os.path.join(workspace["ws"], "a.py")) is True
        assert c.permits_read(os.path.join(workspace["ws"], "sub", "b.py")) is True
        assert c.permits_read(os.path.join(workspace["sibling"], "secret.txt")) is False
        # A sibling whose name PREFIXES the root string must not slip through a
        # naive startswith without the separator (``/ws`` vs ``/ws-evil``).
        assert c.permits_read(workspace["ws"] + "-evil/x") is False

    def test_scratch_and_plan_carveout_permitted(self, workspace):
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        assert c.permits_write("/tmp/mewbo/widgets/w.py") is True
        assert c.permits_write(os.path.join(PLAN_DIR_ROOT, "s", "plan.md")) is True

    def test_symlink_escape_rejected(self, workspace):
        # A symlink INSIDE the workspace pointing OUT of it must not launder an
        # escape — the physical realpath target is authoritative.
        link = os.path.join(workspace["ws"], "escape")
        os.symlink(workspace["sibling"], link)
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        assert c.permits_read(os.path.join(link, "secret.txt")) is False

    def test_read_only_forbids_all_writes(self, workspace):
        ro = WorkspaceContainment(mode="read_only", root=workspace["ws"])
        # Even INSIDE the workspace, a read_only agent may not write.
        assert ro.permits_write(os.path.join(workspace["ws"], "a.py")) is False
        assert ro.permits_write("/tmp/mewbo/x") is False
        # Reads inside are still fine.
        assert ro.permits_read(os.path.join(workspace["ws"], "a.py")) is True

    def test_full_access_permits_anything(self):
        c = WorkspaceContainment(mode="full_access", root="/ws")
        assert c.permits_read("/etc/passwd") is True
        assert c.permits_write("/anywhere/at/all") is True


# ---------------------------------------------------------------------------
# (1)(2)(3) resolve_safe_path under an explicit active containment
# ---------------------------------------------------------------------------


class TestResolveSafePathContained:
    def teardown_method(self):
        # ``set_config_override`` is process-global and nothing in ``conftest``
        # resets it, so a case that pins an axis must hand the config back.
        reset_config()

    def test_workspace_write_read_inside_ok(self, workspace):
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        got = resolve_safe_path("a.py", root=workspace["ws"], containment=c)
        assert str(got) == os.path.join(workspace["ws"], "a.py")

    @pytest.mark.parametrize(
        "outside",
        ["sibling_secret", "/app/config.yaml", "/home/other/.aws/credentials"],
    )
    def test_workspace_write_read_outside_denied(self, workspace, outside):
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        target = (
            os.path.join(workspace["sibling"], "secret.txt")
            if outside == "sibling_secret"
            else outside
        )
        with pytest.raises(ValueError) as exc:
            resolve_safe_path(target, root=workspace["ws"], containment=c)
        # A denial names what IS allowed so the model restages in one turn.
        assert workspace["ws"] in str(exc.value)

    def test_workspace_write_write_outside_denied(self, workspace):
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        with pytest.raises(ValueError):
            resolve_safe_path(
                os.path.join(workspace["sibling"], "new.py"),
                root=workspace["ws"],
                containment=c,
                write=True,
            )

    def test_read_only_write_denied_read_confined(self, workspace):
        ro = WorkspaceContainment(mode="read_only", root=workspace["ws"])
        # A write anywhere (even inside) raises under read_only.
        with pytest.raises(ValueError):
            resolve_safe_path("a.py", root=workspace["ws"], containment=ro, write=True)
        # Reads inside resolve; reads outside raise.
        assert resolve_safe_path("a.py", root=workspace["ws"], containment=ro)
        with pytest.raises(ValueError):
            resolve_safe_path(
                os.path.join(workspace["sibling"], "secret.txt"),
                root=workspace["ws"],
                containment=ro,
            )

    def test_active_containment_via_contextvar_fallback(self, workspace):
        # No explicit containment arg — the loop-established active containment
        # is picked up as the fallback.
        c = WorkspaceContainment(mode="workspace_write", root=workspace["ws"])
        assert get_active_containment() is None
        with active_containment(c):
            assert get_active_containment() is c
            assert resolve_safe_path("a.py", root=workspace["ws"])
            with pytest.raises(ValueError):
                resolve_safe_path(
                    os.path.join(workspace["sibling"], "secret.txt"),
                    root=workspace["ws"],
                )
        # Context resets on exit — no leak.
        assert get_active_containment() is None

    def test_inactive_full_access_containment_is_noop(self, workspace):
        # An explicit full_access containment must NOT divert to the strict path;
        # it resolves through the ordinary tenant union (root arg inserted).
        #
        # This case exercises ``workspace_enforcement``, not path scoping; pin the
        # path-scope axis off so it is testing one thing. ``workspace`` builds
        # under ``tmp_path`` and binds no project, so with
        # ``path_scope_to_active_project`` at its shipped default (True) the
        # ``root`` argument here is a WIDENING one and is correctly ignored —
        # which would make this assertion fail for a reason that has nothing to
        # do with containment. The scoped behaviour has its own coverage in
        # ``tests/test_path_guard_scope_parity.py``.
        set_config_override({"agent": {"path_scope_to_active_project": False}})
        c = WorkspaceContainment(mode="full_access", root=workspace["ws"])
        got = resolve_safe_path("a.py", root=workspace["ws"], containment=c)
        assert str(got) == os.path.join(workspace["ws"], "a.py")
        # And a full_access containment does not confine an out-of-union path any
        # differently than the union already would (union still governs).
        with active_containment(WorkspaceContainment(mode="full_access", root="/ws")):
            assert get_active_containment() is None  # inactive → never set


# ---------------------------------------------------------------------------
# (4) agent.workspace_enforcement OFF: byte-identical to the pre-existing union
# ---------------------------------------------------------------------------


class TestFlagOffByteIdentical:
    """The flag is ``agent.workspace_enforcement`` — NOT ``path_scope_to_active_project``.

    Naming it matters because the two default OPPOSITE ways:
    ``workspace_enforcement`` is the one this class turns off, while
    ``path_scope_to_active_project`` ships **on**. A reader who assumed the
    second is what "flag off" meant would read the case below as proof that a
    model-supplied ``root`` widens the scope under the SHIPPED default — which
    is exactly the hole that made the defect invisible for as long as it was.
    """

    def teardown_method(self):
        reset_config()

    def test_union_still_honors_explicit_root(self, workspace):
        # With no containment at all, an explicit root widens the union exactly
        # as before — a path under it resolves.
        #
        # This case exercises ``workspace_enforcement``, not path scoping; pin
        # the path-scope axis off so it is testing one thing. Under the shipped
        # default this ``root`` is a WIDENING one (nothing published, and the
        # workspace is not a configured project), so it is correctly ignored —
        # asserted from the other side in
        # ``tests/test_path_guard_scope_parity.py``.
        set_config_override({"agent": {"path_scope_to_active_project": False}})
        got = resolve_safe_path("a.py", root=workspace["ws"])
        assert str(got) == os.path.join(workspace["ws"], "a.py")

    def test_union_still_rejects_unrooted_outside(self, workspace):
        # A sibling under no allowed root is rejected by the union, unchanged.
        with pytest.raises(ValueError):
            resolve_safe_path(os.path.join(workspace["sibling"], "secret.txt"))


# ---------------------------------------------------------------------------
# (5)(7) monotonic + independent narrowing via child()
# ---------------------------------------------------------------------------


class TestWorkspaceModeNarrowing:
    def test_rank_table_matches_agent_context(self):
        assert AgentContext._WORKSPACE_MODE_RANK == WORKSPACE_MODE_RANK

    def test_root_default_full_access(self):
        assert AgentContext.root(model_name="m").workspace_mode == "full_access"

    def test_child_narrows_min_wins(self):
        root = AgentContext.root(model_name="m")  # full_access
        assert root.child(workspace_mode="workspace_write").workspace_mode == "workspace_write"
        assert root.child(workspace_mode="read_only").workspace_mode == "read_only"

    def test_child_cannot_widen(self):
        root = AgentContext.root(model_name="m", workspace_mode="workspace_write")
        # A child requesting the wider full_access stays at the parent's tier.
        assert root.child(workspace_mode="full_access").workspace_mode == "workspace_write"

    def test_deep_nesting_monotonic(self):
        # A grandchild under a read_only ancestor requesting full_access stays
        # read_only (min-wins compounds down the chain).
        root = AgentContext.root(model_name="m")
        child = root.child(workspace_mode="read_only")
        grandchild = child.child(workspace_mode="full_access")
        assert grandchild.workspace_mode == "read_only"

    def test_unknown_collapses_to_widest(self):
        root = AgentContext.root(model_name="m")
        # A garbage request neither tightens surprisingly nor loosens below parent.
        assert root.child(workspace_mode="bogus").workspace_mode == "full_access"
        narrowed = AgentContext.root(model_name="m", workspace_mode="read_only")
        assert narrowed.child(workspace_mode="bogus").workspace_mode == "read_only"

    @pytest.mark.parametrize(
        "cap_parent,cap_req,cap_exp,ws_parent,ws_req,ws_exp",
        [
            ("all", "read_only", "read_only", "full_access", "full_access", "full_access"),
            ("all", "all", "all", "full_access", "read_only", "read_only"),
            ("read_only", "all", "read_only", "workspace_write", "read_only", "read_only"),
            ("execute", "execute", "execute", "full_access", "workspace_write", "workspace_write"),
        ],
    )
    def test_axes_narrow_independently(
        self, cap_parent, cap_req, cap_exp, ws_parent, ws_req, ws_exp
    ):
        # capability_mode and workspace_mode are ORTHOGONAL — narrowing one never
        # perturbs the other (shared ``_narrow``, two rank tables).
        parent = AgentContext.root(
            model_name="m", workspace_mode=ws_parent
        )
        # Seed the parent's capability_mode by narrowing once from an all root.
        parent = parent.child(capability_mode=cap_parent, workspace_mode=ws_parent)
        child = parent.child(capability_mode=cap_req, workspace_mode=ws_req)
        assert child.capability_mode == AgentContext._narrow(
            cap_parent, cap_req, AgentContext._CAPABILITY_MODE_RANK
        )
        assert child.workspace_mode == AgentContext._narrow(
            ws_parent, ws_req, AgentContext._WORKSPACE_MODE_RANK
        )


# ---------------------------------------------------------------------------
# (6)(9) loop-level: authoritative injection + cwd → containment root
# ---------------------------------------------------------------------------


def _build_loop(*, workspace_mode: str, cwd: str, enforcement: bool | None):
    """Construct a ToolUseLoop with the enforcement flag set for the run.

    *enforcement* of ``None`` leaves the config UNTOUCHED — the run exercises
    whatever ``agent.workspace_enforcement`` actually ships as (the
    ``app_config_file`` fixture writes real ``AppConfig()`` defaults to disk),
    rather than a value this helper forced. Use ``None`` for a test that must
    prove something about the SHIPPED default; an explicit ``True``/``False``
    is for tests that need a specific value regardless of what ships.

    Imported lazily + patched like the sibling loop tests so no real model is
    built.
    """
    from unittest.mock import MagicMock, patch

    from mewbo_core.agents.hypervisor import AgentHypervisor
    from mewbo_core.loop.tool_use_loop import ToolUseLoop

    # Reuse the sibling suite's tool/registry/policy/hook fakes.
    from test_tool_use_loop import (  # type: ignore[import-not-found]
        _allow_all_policy,
        _make_hook_manager,
        _make_registry,
        _make_spec,
    )

    if enforcement is not None:
        set_config_override({"agent": {"workspace_enforcement": enforcement}})
    ctx = AgentContext.root(
        model_name="m",
        registry=AgentHypervisor(max_concurrent=100),
        workspace_mode=workspace_mode,
    )
    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = MagicMock()
        return ToolUseLoop(
            agent_context=ctx,
            tool_registry=_make_registry(_make_spec("read_file", "Read a file")),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            cwd=cwd,
        )


class TestLoopContainmentSeam:
    def teardown_method(self):
        reset_config()

    def test_containment_built_when_enforced_and_narrowed(self, workspace):
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=workspace["ws"], enforcement=True
        )
        assert loop._containment is not None
        # (9) the anchored cwd IS the containment root.
        assert loop._containment.root == workspace["ws"]
        assert loop._containment.mode == "workspace_write"

    def test_no_containment_when_flag_off(self, workspace):
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=workspace["ws"], enforcement=False
        )
        assert loop._containment is None

    def test_no_containment_when_full_access(self, workspace):
        loop = _build_loop(
            workspace_mode="full_access", cwd=workspace["ws"], enforcement=True
        )
        assert loop._containment is None

    def test_no_containment_when_no_cwd(self):
        loop = _build_loop(workspace_mode="read_only", cwd="", enforcement=True)
        assert loop._containment is None

    def test_authoritative_injection_overrides_model_root(self, workspace):
        # (6) Under active containment, a model-supplied WIDER root is IGNORED —
        # the workspace root is forced.
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=workspace["ws"], enforcement=True
        )
        step = loop._tool_call_to_action_step(
            {"name": "read_file", "args": {"path": "a.py", "root": "/somewhere/wide"}, "id": "1"}
        )
        assert step.tool_input["root"] == workspace["ws"]

    def test_advisory_injection_when_off_honors_model_root(self, workspace):
        # Flag off → today's advisory behaviour: a model-supplied root is honored.
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=workspace["ws"], enforcement=False
        )
        step = loop._tool_call_to_action_step(
            {"name": "read_file", "args": {"path": "a.py", "root": "/model/root"}, "id": "1"}
        )
        assert step.tool_input["root"] == "/model/root"

    def test_advisory_injection_when_off_fills_missing_root(self, workspace):
        loop = _build_loop(
            workspace_mode="full_access", cwd=workspace["ws"], enforcement=True
        )
        step = loop._tool_call_to_action_step(
            {"name": "read_file", "args": {"path": "a.py"}, "id": "1"}
        )
        assert step.tool_input["root"] == workspace["ws"]


# ---------------------------------------------------------------------------
# The escape regression — reproduces a cross-tenant read, against
# the config the ``app_config_file`` fixture actually WRITES (real ``AppConfig()``
# defaults), not a value this suite forces. The vulnerable surface is the
# tenant UNION in ``_get_allowed_roots()`` (cwd ∪ every configured project path
# ∪ scratch): a single directory is not enough to reproduce it, because the
# union already refuses a path that belongs to no configured root regardless of
# enforcement. Two REGISTERED projects are required so the read actually lands
# inside the union the historical behaviour granted.
# ---------------------------------------------------------------------------


@pytest.fixture
def tenants(tmp_path):
    """Two configured projects, each owning a file only its own tenant should read."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "own.py").write_text("mine = True\n")
    beta_secret = beta / "credentials.env"
    beta_secret.write_text("SECRET=hunter2\n")
    set_config_override(
        {"projects": {"alpha": {"path": str(alpha)}, "beta": {"path": str(beta)}}}
    )
    return {"alpha": str(alpha), "beta": str(beta), "beta_secret": str(beta_secret)}


class TestTenantUnionEscapeAtShippedDefaults:
    """The primary artifact for the cross-tenant escape: an attempted escape, and its refusal.

    A ``workspace_write`` child rooted at project ``alpha`` must not be able to
    read project ``beta``'s file via the historical tenant-union allowlist —
    neither through the root the LOOP injects, nor through a wider root the
    MODEL supplies. Both vectors are exercised because the loop's authoritative
    injection only overrides a model-supplied root once containment is
    active; a test that only tries the loop-injected vector would miss a
    regression in that override.
    """

    def teardown_method(self):
        reset_config()

    def test_shipped_default_is_enforcement_on(self):
        # Pin the default itself, so a change here is loud rather than a test
        # elsewhere silently starting to exercise a different value.
        assert AppConfig().agent.workspace_enforcement is True

    def test_loop_injected_root_refuses_cross_tenant_read(self, tenants):
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=tenants["alpha"], enforcement=None
        )
        step = loop._tool_call_to_action_step(
            {"name": "read_file", "args": {"path": tenants["beta_secret"]}, "id": "1"}
        )
        assert step.tool_input["root"] == tenants["alpha"]
        with active_containment(loop._containment):
            with pytest.raises(ValueError) as exc:
                resolve_safe_path(step.tool_input["path"], root=step.tool_input["root"])
        assert tenants["alpha"] in str(exc.value)

    def test_model_supplied_wider_root_is_ignored_not_honored(self, tenants):
        # The escape as the issue describes it: the model names the OTHER
        # tenant's project as `root` outright, trying to widen its own reach.
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=tenants["alpha"], enforcement=None
        )
        step = loop._tool_call_to_action_step(
            {
                "name": "read_file",
                "args": {"path": tenants["beta_secret"], "root": tenants["beta"]},
                "id": "1",
            }
        )
        # Authoritative injection overrides the model's requested root outright.
        assert step.tool_input["root"] == tenants["alpha"]
        with active_containment(loop._containment):
            with pytest.raises(ValueError):
                resolve_safe_path(step.tool_input["path"], root=step.tool_input["root"])

    def test_escape_reproduces_only_when_enforcement_explicitly_disabled(self, tenants):
        # Documents the hole the flip closed: the identical request the two
        # tests above refuse SUCCEEDS once an operator opts back into the
        # historical union by turning enforcement off explicitly.
        loop = _build_loop(
            workspace_mode="workspace_write", cwd=tenants["alpha"], enforcement=False
        )
        step = loop._tool_call_to_action_step(
            {"name": "read_file", "args": {"path": tenants["beta_secret"]}, "id": "1"}
        )
        with active_containment(loop._containment):
            got = resolve_safe_path(step.tool_input["path"], root=step.tool_input["root"])
        assert str(got) == tenants["beta_secret"]


# ---------------------------------------------------------------------------
# read_only write-tier regression — resolve_safe_path's own write= arm was
# always correct (see TestResolveSafePathContained above); the defect was that
# every PRODUCTION write call site omitted write=, so permits_write() was
# unreachable from any real tool and read_only silently behaved as
# workspace_write. Drives the real tool/function call sites, not the raw guard.
# ---------------------------------------------------------------------------


class TestReadOnlyWriteRefusedAtProductionSites:
    def teardown_method(self):
        reset_config()

    def test_file_edit_tool_write_refused_under_read_only(self, workspace):
        from mewbo_core.contracts.errors import ToolInputError
        from mewbo_tools.integration.edit_common import resolve_and_validate_path

        ro = WorkspaceContainment(mode="read_only", root=workspace["ws"])
        with active_containment(ro):
            with pytest.raises(ToolInputError):
                resolve_and_validate_path("a.py", workspace["ws"], write=True)
            # The read/validate arm (get_state) stays permitted.
            resolve_and_validate_path("a.py", workspace["ws"], write=False)

    def test_search_replace_apply_refused_under_read_only(self, workspace):
        from mewbo_tools.aider_bridge.edit_blocks import apply_search_replace_blocks

        target = os.path.join(workspace["ws"], "a.py")
        block = f"{target}\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        ro = WorkspaceContainment(mode="read_only", root=workspace["ws"])
        with active_containment(ro):
            with pytest.raises(ValueError):
                apply_search_replace_blocks(block, root=workspace["ws"], write=True)
        # File on disk is untouched — the write never landed.
        assert open(target, encoding="utf-8").read() == "x = 1\n"
