#!/usr/bin/env python3
"""``lsp_tool`` resolves every path through the guard — no unguarded branch.

The LSP tool is model-callable and content-returning: ``diagnostics`` opens the
file (``LSPServerManager.open_file`` reads it and ships the text to the server)
and ``hover`` returns server output derived from it. It used to route through
``resolve_safe_path`` ONLY while a ``WorkspaceContainment`` was active — and a
containment is built only for an agent narrower than ``full_access``, while the
ROOT agent an operator drives is always ``full_access``. So the session that
matters took the ``else`` branch: a bare ``Path(file_path).resolve()``, i.e. an
arbitrary-file read that the shell (Landlock) and ``read_file`` (the path guard,
scoped by ``agent.path_scope_to_active_project``) both refuse.

What is pinned here:

1. With a full_access session (no containment) anchored at project A, an LSP
   query on project B is REFUSED, and the refusal NAMES A so the model restages
   in one turn rather than hunting for a way around the guard.
2. A query inside A still resolves — the guard closes an escape, not the tool.
3. The pre-existing narrowed-containment path is unchanged, including a relative
   ``file_path`` resolving against the containment's own root rather than the
   server process's cwd.
4. With nothing published (a direct library caller, a test) the historical union
   still applies, so the change is a scope and not a blanket refusal.

Driven through ``LSPTool.run`` with the contextvars the loop publishes; the
manager is stubbed, so no language server is ever spawned.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.config import reset_config, set_config_override
from mewbo_core.workspaces.workspace import (
    WorkspaceContainment,
    active_containment,
    active_project_root,
)
from mewbo_tools.integration.lsp.tool import LSPTool


@pytest.fixture
def tenants(tmp_path, app_config_file):
    """Two configured projects; A is the session's, B is the other tenant."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    for d in (alpha, beta):
        d.mkdir()
    (alpha / "own.py").write_text("mine = True\n")
    (beta / "credentials.env").write_text("SECRET=hunter2\n")
    set_config_override(
        {"projects": {"alpha": {"path": str(alpha)}, "beta": {"path": str(beta)}}}
    )
    yield {
        "alpha": str(alpha),
        "alpha_own": str(alpha / "own.py"),
        "beta": str(beta),
        "beta_secret": str(beta / "credentials.env"),
    }
    reset_config()


def _tool() -> LSPTool:
    return LSPTool.__new__(LSPTool)  # skip __init__ (no AbstractTool wiring)


def _step(file_path: str, operation: str = "diagnostics") -> ActionStep:
    return ActionStep(
        title="lsp",
        tool_id="lsp",
        operation=operation,
        tool_input={"operation": operation, "file_path": file_path, "line": 0, "character": 0},
    )


def _run(file_path: str, operation: str = "diagnostics") -> str:
    """Run the tool with the manager stubbed out; return the rendered content.

    ``server_for_file`` returning ``None`` makes a path that PASSES the guard
    land on "no language server configured", which is how a pass is told apart
    from a denial without spawning anything.
    """
    mgr = MagicMock()
    mgr.server_for_file.return_value = None
    with (
        patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr),
        patch("mewbo_tools.integration.lsp.tool.run_lsp_async", return_value=None),
    ):
        return str(_tool().run(_step(file_path, operation)).content)


class TestFullAccessSessionIsScopedToItsProject:
    """(1) The escape: no containment, so the old code resolved unguarded."""

    @pytest.mark.parametrize("operation", ["diagnostics", "hover"])
    def test_another_tenants_file_is_refused(self, tenants, operation):
        with active_project_root(tenants["alpha"]):
            content = _run(tenants["beta_secret"], operation)
        assert "Path not permitted" in content
        allowed = content.split("Allowed roots: ")[1]
        assert tenants["alpha"] in allowed, "a denial must name what IS allowed"
        assert tenants["beta"] not in allowed

    def test_the_harness_root_is_refused(self, tenants):
        # ``os.getcwd()`` is the harness itself on a container deployment, and
        # the union would otherwise hand a session its own config/source.
        import os

        with active_project_root(tenants["alpha"]):
            content = _run(os.path.join(os.getcwd(), "pyproject.toml"))
        assert "Path not permitted" in content

    def test_no_containment_is_active_in_this_scenario(self, tenants):
        # Guards the test above against passing for the wrong reason: if a
        # containment were somehow active, the OLD code would have denied too.
        from mewbo_core.workspaces.workspace import get_active_containment

        with active_project_root(tenants["alpha"]):
            assert get_active_containment() is None


class TestTheSessionKeepsItsOwnProject:
    """(2) No regression: the legitimate query still reaches the manager."""

    def test_own_project_file_resolves(self, tenants):
        with active_project_root(tenants["alpha"]):
            content = _run(tenants["alpha_own"])
        assert "Path not permitted" not in content
        assert "No language server configured" in content

    def test_relative_path_resolves_against_the_active_project_root(self, tenants):
        # The base for a relative path is the session's project, NOT the server
        # process's cwd — which on the deployed shape is the harness.
        with active_project_root(tenants["alpha"]):
            content = _run("own.py")
        assert "Path not permitted" not in content


class TestNarrowedContainmentUnchanged:
    """(3) The pre-existing containment path behaves exactly as before."""

    def test_outside_the_workspace_is_denied(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "x.py").write_text("y = 2\n")
        with active_containment(WorkspaceContainment(mode="read_only", root=str(ws))):
            content = _run(str(outside / "x.py"))
        assert "Path not permitted" in content
        assert str(ws) in content

    def test_inside_the_workspace_resolves(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "foo.py").write_text("z = 3\n")
        with active_containment(WorkspaceContainment(mode="read_only", root=str(ws))):
            content = _run(str(ws / "foo.py"))
        assert "Path not permitted" not in content

    def test_relative_path_resolves_against_the_workspace_root(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "foo.py").write_text("z = 3\n")
        with active_containment(WorkspaceContainment(mode="read_only", root=str(ws))):
            content = _run("foo.py")
        assert "Path not permitted" not in content

    def test_containment_wins_over_the_active_project_root(self, tmp_path, tenants):
        # Both published: the narrower containment decides, so a narrowed
        # sub-agent cannot widen itself back out to the session's project.
        ws = tmp_path / "ws"
        ws.mkdir()
        with (
            active_project_root(tenants["alpha"]),
            active_containment(WorkspaceContainment(mode="read_only", root=str(ws))),
        ):
            content = _run(tenants["alpha_own"])
        assert "Path not permitted" in content


class TestNothingPublishedKeepsTheHistoricalUnion:
    """(4) A direct library caller or a test is not newly broken."""

    def test_a_configured_project_still_resolves(self, tenants):
        content = _run(tenants["alpha_own"])
        assert "Path not permitted" not in content

    def test_the_other_tenant_also_resolves_unscoped(self, tenants):
        # The union is byte-identical when nothing is published — stated so the
        # scoped denial above is measured against a path that WAS reachable.
        content = _run(tenants["beta_secret"])
        assert "Path not permitted" not in content
