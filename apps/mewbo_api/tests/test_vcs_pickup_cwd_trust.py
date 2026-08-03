"""A PR-head worktree's own ``.mcp.json`` must never reach the merged config.

The pickup harness runs a session whose ``cwd`` is a worktree checked out on a
pull request's head branch — content authored by whoever opened the PR. That is
the premise of the feature, not an edge case, so the directory is untrusted by
construction.

The property is not "the tool is not callable". A merged-config server entry is
a ``command`` the process SPAWNS while resolving config, before any tool ceiling
is consulted, so the only assertion worth making is that the entry never reaches
the config at all.

This drives the real :meth:`VcsPickupService.ensure_worktree` with only the
project store stubbed (no git, no network), then asks the REAL merged-config
resolver what the worktree contributes — so it fails if the marking is dropped,
and it would also fail if marking stopped being sufficient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mewbo_api import vcs_pickup
from mewbo_core.config import (
    _UNTRUSTED_CWDS,
    get_merged_mcp_config,
    reset_config,
    set_mcp_config_path,
)

HOSTILE = "planted_by_the_pr_author"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path):
    """Empty operator config, and no untrusted-root leakage between tests."""
    _UNTRUSTED_CWDS.clear()
    global_mcp = tmp_path / "operator_mcp.json"
    global_mcp.write_text(json.dumps({"servers": {}}), encoding="utf-8")
    set_mcp_config_path(str(global_mcp))
    yield
    reset_config()
    _UNTRUSTED_CWDS.clear()


class _Target:
    """The managed parent project a worktree is cut from."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.project_id = "parent-1"


class _Worktree:
    def __init__(self, path: str) -> None:
        self.path = path
        self.project_id = "wt-1"
        self.branch = BRANCH
        self.is_worktree = True


BRANCH = "feature/from-a-stranger"


def _prepare(worktree: Path, tmp_path: Path, monkeypatch) -> None:
    """Run the real ``ensure_worktree`` over *worktree*."""
    _service_over(worktree, monkeypatch).ensure_worktree(_Target(str(tmp_path / "parent")), BRANCH)


def _service_over(worktree_path: Path, monkeypatch) -> vcs_pickup.VcsPickupService:
    """The real service, with only the git/store boundary stubbed."""
    svc = vcs_pickup._service

    class _Store:
        @staticmethod
        def create_worktree(_project_id, _branch, **_kw):
            return _Worktree(str(worktree_path))

    monkeypatch.setattr(svc, "project_store", _Store())
    monkeypatch.setattr(svc, "_ensure_local_branch", lambda *_a, **_k: None)
    monkeypatch.setattr(svc, "_git_ok", lambda *_a, **_k: True)
    return svc


def _plant(worktree: Path, relative: str) -> None:
    target = worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"servers": {HOSTILE: {"command": "sh", "args": ["-c", "true"]}}}),
        encoding="utf-8",
    )


@pytest.mark.parametrize("planted_at", [".mcp.json", "tools/.mcp.json"])
def test_a_pr_worktree_contributes_no_mcp_server(tmp_path, monkeypatch, planted_at):
    """Root and subtree alike — one file in one subdirectory is enough."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _plant(worktree, planted_at)

    # Positive control FIRST: before the worktree is prepared, this directory
    # would contribute its server. Without this the assertion below could pass
    # because the file was unreadable rather than because it was excluded.
    before = get_merged_mcp_config(cwd=str(worktree), trust_cwd=True).get("servers", {})
    assert HOSTILE in before, "the planted config was not readable — test proves nothing"

    _prepare(worktree, tmp_path, monkeypatch)

    # Note `trust_cwd=True`: the caller asked for the permissive path, exactly
    # as the un-threaded pickup call site does today. The mark must still win.
    after = get_merged_mcp_config(cwd=str(worktree), trust_cwd=True).get("servers", {})
    assert HOSTILE not in after


def test_the_mark_covers_paths_below_the_worktree(tmp_path, monkeypatch):
    """A session cwd deeper than the worktree root is covered too.

    Registration is by root, and containment is what makes that enough — a
    per-directory mark would miss any nested cwd a future caller chose.
    """
    worktree = tmp_path / "wt"
    nested = worktree / "packages" / "inner"
    nested.mkdir(parents=True)
    _plant(nested, ".mcp.json")

    assert HOSTILE in get_merged_mcp_config(cwd=str(nested), trust_cwd=True).get("servers", {})

    _prepare(worktree, tmp_path, monkeypatch)

    assert HOSTILE not in get_merged_mcp_config(cwd=str(nested), trust_cwd=True).get("servers", {})
