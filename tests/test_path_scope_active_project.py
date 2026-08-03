#!/usr/bin/env python3
"""Active-project scoping for the PATH-TAKING tools.

The shell is confined at the kernel (``ShellScope``/Landlock), but every tool
that takes a path ARGUMENT — file read, file edit, directory listing, LSP —
resolved through the tenant union: CWD ∪ every configured project.
A root agent is always ``full_access``, so ``WorkspaceContainment`` never
applies to the session an operator drives, and the union therefore let
``read_file`` reach another project's checkout and the harness's own config
directory — routing around the sandbox by using ``read_file`` instead of
``cat``.

What is pinned here:

1. A session anchored at project A cannot resolve into project B, nor into the
   harness root the process CWD derives.
2. It CAN resolve its own project, a worktree under
   ``<A>/.mewbo/worktrees/<name>``, the Mewbo scratch roots, and any
   ``allowed_paths`` entry A re-admits — the same carve-outs the shell sandbox
   grants, resolved by the SAME ``ShellScope.readmitted_for`` so the two guards
   cannot disagree about whose project this is.
3. The scope never comes from a tool ARGUMENT. With no containment the loop
   passes a model-supplied ``root`` straight through, so an out-of-scope one is
   ignored rather than honoured.
4. Both "off" paths leave the union byte-identical: the flag
   disabled, and no active project root published at all (a direct library
   caller or a test).

Driven through ``resolve_safe_path``/``_get_allowed_roots`` with the contextvar
the loop publishes, never by standing up a real LLM turn.
"""

from __future__ import annotations

import os

import pytest
from mewbo_core.config import AppConfig, reset_config, set_config_override
from mewbo_core.tooling.exit_plan_mode import PLAN_DIR_ROOT
from mewbo_core.workspaces.workspace import active_project_root
from mewbo_tools.core import _get_allowed_roots, resolve_safe_path
from mewbo_tools.integration.landlock import ShellScope


@pytest.fixture
def tenants(tmp_path, app_config_file):
    """Two configured projects, a shared dir A re-admits, and a worktree in A."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    shared = tmp_path / "shared_data"
    worktree = alpha / ".mewbo" / "worktrees" / "x"
    for d in (alpha, beta, shared, worktree):
        d.mkdir(parents=True)
    (alpha / "own.py").write_text("mine = True\n")
    (beta / "credentials.env").write_text("SECRET=hunter2\n")
    (shared / "shared.txt").write_text("SHARED\n")
    (worktree / "w.py").write_text("wt = True\n")
    set_config_override(
        {
            "projects": {
                "alpha": {"path": str(alpha), "allowed_paths": [str(shared)]},
                "beta": {"path": str(beta)},
            }
        }
    )
    return {
        "alpha": str(alpha),
        "alpha_own": str(alpha / "own.py"),
        "beta": str(beta),
        "beta_secret": str(beta / "credentials.env"),
        "shared": str(shared / "shared.txt"),
        "worktree": str(worktree),
        "worktree_file": str(worktree / "w.py"),
    }


class TestShippedDefault:
    def teardown_method(self):
        reset_config()

    def test_scoping_is_on_by_default(self):
        # Pinned here so a change to the default is loud, rather than a test
        # elsewhere silently starting to exercise the other behaviour.
        assert AppConfig().agent.path_scope_to_active_project is True

    def test_it_ships_with_the_same_value_as_the_shell_sandbox(self):
        # Two switches, one boundary: they may be turned off independently, but
        # a deployment that changes neither must get both halves.
        agent = AppConfig().agent
        assert agent.path_scope_to_active_project == agent.shell_sandbox


class TestScopedSessionCannotReachOtherTenants:
    """(1) The escape this closes, from the session an operator actually drives."""

    def teardown_method(self):
        reset_config()

    def test_another_project_is_refused(self, tenants):
        with active_project_root(tenants["alpha"]):
            with pytest.raises(ValueError) as exc:
                resolve_safe_path(tenants["beta_secret"])
        allowed = str(exc.value).split("Allowed roots: ")[1]
        assert tenants["alpha"] in allowed, "a denial must name what IS allowed"
        assert tenants["beta"] not in allowed

    def test_the_harness_root_is_refused(self, tenants):
        # ``os.getcwd()`` is the harness itself on a container deployment, so an
        # unscoped union hands a session its own config file.
        target = os.path.join(os.getcwd(), "pyproject.toml")
        with active_project_root(tenants["alpha"]):
            with pytest.raises(ValueError):
                resolve_safe_path(target)

    def test_the_harness_root_is_reachable_without_scoping(self, tenants):
        # The same read succeeds unscoped — so the test above is measuring the
        # scope and not a path that was unreachable anyway.
        assert str(resolve_safe_path(os.path.join(os.getcwd(), "pyproject.toml")))

    def test_an_unbound_session_reaches_no_configured_project(self, tenants, tmp_path):
        # The auto context: a scratch cwd with no project bound sees only itself,
        # matching what the shell sandbox denies (every configured project).
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        with active_project_root(str(scratch)):
            with pytest.raises(ValueError):
                resolve_safe_path(tenants["alpha_own"])
            with pytest.raises(ValueError):
                resolve_safe_path(tenants["beta_secret"])


class TestScopedSessionKeepsItsOwnReach:
    """(2) Everything the shell sandbox re-admits still resolves."""

    def teardown_method(self):
        reset_config()

    @pytest.mark.parametrize(
        "key", ["alpha_own", "worktree_file", "shared"]
    )
    def test_own_project_worktree_and_allowed_paths_resolve(self, tenants, key):
        with active_project_root(tenants["alpha"]):
            assert str(resolve_safe_path(tenants[key])) == tenants[key]

    def test_scratch_roots_stay_reachable(self, tenants):
        with active_project_root(tenants["alpha"]):
            assert str(resolve_safe_path("/tmp/mewbo/widgets/w.py"))
            assert str(resolve_safe_path(os.path.join(PLAN_DIR_ROOT, "s", "plan.md")))

    def test_a_session_anchored_at_its_worktree_keeps_the_worktree(self, tenants):
        # A worktree lives INSIDE its project, and the shell sandbox denies the
        # project while carving the worktree back out. The guard agrees: the
        # published root is what counts, whichever of the two it names.
        with active_project_root(tenants["worktree"]):
            assert str(resolve_safe_path(tenants["worktree_file"]))
            with pytest.raises(ValueError):
                resolve_safe_path(tenants["alpha_own"])

    def test_the_two_guards_resolve_the_same_project(self, tenants):
        # DRY, asserted rather than assumed: one resolution feeds both halves,
        # so neither can drift on "which project is this session's".
        readmitted = ShellScope.readmitted_for(tenants["alpha"])
        assert readmitted == (tenants["alpha"], os.path.dirname(tenants["shared"]))
        with active_project_root(tenants["alpha"]):
            roots = [str(r) for r in _get_allowed_roots()]
        assert roots == [*readmitted, PLAN_DIR_ROOT, "/tmp/mewbo"]


class TestTheModelCannotWidenItsScope:
    """(3) An out-of-scope ``root`` ARGUMENT is ignored, not honoured."""

    def teardown_method(self):
        reset_config()

    def test_a_model_supplied_root_cannot_reach_another_project(self, tenants):
        # With no containment the loop honours a model-supplied ``root``, so the
        # guard is the only thing standing between it and a chosen sandbox.
        with active_project_root(tenants["alpha"]):
            with pytest.raises(ValueError):
                resolve_safe_path("credentials.env", root=tenants["beta"])
            with pytest.raises(ValueError):
                resolve_safe_path(tenants["beta_secret"], root=tenants["beta"])

    def test_a_root_that_narrows_is_still_honoured(self, tenants):
        # Narrowing is not widening: the worktree is inside the active project,
        # and a relative path must still resolve against the root it was given.
        with active_project_root(tenants["alpha"]):
            got = resolve_safe_path("w.py", root=tenants["worktree"])
        assert str(got) == tenants["worktree_file"]

    def test_an_out_of_scope_root_is_honoured_when_unscoped(self, tenants):
        # The advisory behaviour where scoping does not apply.
        got = resolve_safe_path("credentials.env", root=tenants["beta"])
        assert str(got) == tenants["beta_secret"]


class TestOffPathsAreByteIdentical:
    """(4) Both ways of not scoping leave the tenant union untouched."""

    def teardown_method(self):
        reset_config()

    def test_no_published_root_leaves_the_union_untouched(self, tenants):
        historical = [str(r) for r in _get_allowed_roots()]
        assert str(os.getcwd()) in historical
        assert tenants["alpha"] in historical
        assert tenants["beta"] in historical
        assert historical[-2:] == [PLAN_DIR_ROOT, "/tmp/mewbo"]
        # And the reads the union granted still succeed.
        assert str(resolve_safe_path(tenants["beta_secret"])) == tenants["beta_secret"]

    def test_flag_off_leaves_the_union_untouched_under_a_published_root(self, tenants):
        unscoped = [str(r) for r in _get_allowed_roots()]
        set_config_override({"agent": {"path_scope_to_active_project": False}})
        with active_project_root(tenants["alpha"]):
            assert [str(r) for r in _get_allowed_roots()] == unscoped
            assert (
                str(resolve_safe_path(tenants["beta_secret"])) == tenants["beta_secret"]
            )

    def test_flag_on_is_what_changes_it(self, tenants):
        # The control for the two tests above: same fixture, same published
        # root, only the flag differs.
        unscoped = [str(r) for r in _get_allowed_roots()]
        with active_project_root(tenants["alpha"]):
            assert [str(r) for r in _get_allowed_roots()] != unscoped
