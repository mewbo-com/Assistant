"""The harness denies itself unconditionally, unless an operator opts out.

``ShellScope.harness_roots`` used to drop any harness root lying inside the
session's active root, so that working ON Mewbo kept its own packages reachable.
Measured on a deployment where the whole checkout is the active root, that
carve-out erased the entire self-deny: ``harness_roots('/app')`` returned nothing
at all, and the config holding the API keys came back into reach.

The carve-out is gone. What replaces it is ``agent.harness_self_deny``, on by
default, because a wrong carve-out is invisible on the box that matters while a
wrong denial is obvious on the box that does not: deny-always fails loudly and
locally, on a workstation, where the person hitting it fixes it in one config
line.

These tests assert the two directions of that switch plus the guard that must
survive it — a denial covering the runtime is still refused, because denying a
root that CONTAINS the interpreter stops every command rather than confining it.
"""

from __future__ import annotations

import os
import sys

import pytest
from mewbo_core.config import AppConfig, reset_config, set_config_override
from mewbo_tools.integration.landlock import ShellScope


def _source_roots() -> tuple[str, set[str]]:
    """The repo root and the harness source trees beneath it, or skip.

    Derived exactly as ``harness_roots`` derives them, from ``mewbo_core``'s own
    location — a pip install has no ``packages`` tree and contributes nothing, so
    there is no ancestor relationship to measure there.
    """
    import mewbo_core

    package_dir = os.path.dirname(os.path.abspath(mewbo_core.__file__))
    parts = package_dir.split(os.sep)
    if "packages" not in parts:
        pytest.skip("not a source checkout, so there is no packages tree to deny")
    repo_root = os.path.realpath(os.sep.join(parts[: parts.index("packages")]))
    roots = {
        os.path.join(repo_root, name)
        for name in ("packages", "apps")
        if os.path.isdir(os.path.join(repo_root, name))
    }
    assert roots, "no harness source tree under the repo root — nothing to measure"
    return repo_root, roots


class TestAnAncestorActiveRootNoLongerErasesTheDenial:
    """The measured defect: the active root containing the harness voided it."""

    @pytest.fixture(autouse=True)
    def _unpin_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolve the config where a real deployment has it: inside the checkout.

        The suite pins ``MEWBO_CONFIG_DIR`` at a throwaway directory outside the
        tree (root ``conftest.py``), which is a genuine extra harness root and
        would keep the return value non-empty for a reason that has nothing to do
        with the axis under test.
        """
        monkeypatch.delenv("MEWBO_CONFIG_DIR", raising=False)

    def teardown_method(self) -> None:
        reset_config()

    def test_the_harness_is_denied_even_inside_the_active_root(self) -> None:
        repo_root, source_roots = _source_roots()
        # The deployed shape: the session's active root IS the checkout holding
        # the harness. Every harness root is then an descendant of it.
        assert ShellScope.harness_roots(repo_root) >= source_roots

    def test_the_active_root_changes_nothing_about_the_answer(self) -> None:
        repo_root, _ = _source_roots()
        assert ShellScope.harness_roots(repo_root) == ShellScope.harness_roots(None)

    def test_denied_for_carries_the_denial_through_to_the_shared_derivation(
        self, tmp_path
    ) -> None:
        repo_root, source_roots = _source_roots()
        # The seam both halves of the boundary read, rather than the helper alone.
        set_config_override({"projects": {"proj": {"path": str(tmp_path)}}})
        expected = ShellScope._survivable(source_roots)
        if not expected:
            pytest.skip("every harness source tree here contains the runtime")
        denied, _allowed = ShellScope.denied_for(repo_root)
        assert expected <= set(denied)


class TestTheOptOutIsExplicit:
    """``agent.harness_self_deny=False`` is the one way to get the old behaviour."""

    def teardown_method(self) -> None:
        reset_config()

    def test_the_flag_off_returns_nothing_at_all(self) -> None:
        set_config_override({"agent": {"harness_self_deny": False}})
        assert ShellScope.harness_roots(None) == set()
        assert ShellScope.harness_roots("/some/active/root") == set()

    def test_the_flag_defaults_on(self) -> None:
        # Read off the real model, never a literal, so flipping the shipped
        # default fails HERE rather than silently unhiding the harness.
        assert AppConfig().agent.harness_self_deny is True


class TestTheRuntimeGuardStillApplies:
    """Non-regression: a denial covering the interpreter is still refused."""

    def teardown_method(self) -> None:
        reset_config()

    def test_a_denial_covering_sys_prefix_is_dropped(self) -> None:
        prefix = os.path.realpath(sys.prefix)
        assert ShellScope._survivable({prefix}) == set()

    def test_the_filesystem_root_is_refused(self) -> None:
        assert ShellScope._survivable({os.sep}) == set()

    def test_an_ordinary_directory_survives(self, tmp_path) -> None:
        # The control: the two refusals above are not vacuously empty.
        ordinary = os.path.realpath(str(tmp_path))
        assert ShellScope._survivable({ordinary}) == {ordinary}
