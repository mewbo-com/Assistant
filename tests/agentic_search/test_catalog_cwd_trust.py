"""A ``project`` taken off a request must never contribute MCP servers.

``SourceCatalog`` and ``WorkspaceMcpConfig`` receive ``project`` straight from
request input — a query-string parameter on the sources/tools routes. Whatever
sits at that path was not authored by this deployment, and a merged-config
server entry is a ``command`` the process SPAWNS during config resolution,
before any tool allowlist is consulted. So the property is not "the tool is not
callable"; it is "the directory contributes no entry at all".

These tests assert the ARGUMENT each seam passes down, driving the real
production classmethods with the resolver stubbed at the one boundary it calls.
Asserting the argument is what makes them fail if someone drops the kwarg — the
regression this file exists to catch. That the argument is load-bearing is
proven separately, against the real merge, in ``tests/test_mcp_cwd_trust.py``.
"""

from __future__ import annotations

import pytest
from mewbo_api.agentic_search import catalog as catalog_mod, mcp_config as mcp_config_mod
from mewbo_api.agentic_search.catalog import SourceCatalog
from mewbo_api.agentic_search.mcp_config import WorkspaceMcpConfig

REQUEST_PATH = "/tmp/attacker-supplied"


class _Recorder:
    """Captures how a resolver was called, and answers with an empty config."""

    def __init__(self, payload: object) -> None:
        self.calls: list[dict] = []
        self._payload = payload

    def __call__(self, cwd=None, *, trust_cwd=None, **kwargs):
        self.calls.append({"cwd": cwd, "trust_cwd": trust_cwd})
        return self._payload

    @property
    def only(self) -> dict:
        assert len(self.calls) == 1, f"expected exactly one resolution, got {self.calls}"
        return self.calls[0]


@pytest.fixture
def merged_config(monkeypatch) -> _Recorder:
    """Stub ``get_merged_mcp_config`` in BOTH modules that import it."""
    rec = _Recorder({"servers": {}})
    monkeypatch.setattr(catalog_mod, "get_merged_mcp_config", rec)
    monkeypatch.setattr(mcp_config_mod, "get_merged_mcp_config", rec)
    return rec


@pytest.fixture
def registry(monkeypatch) -> _Recorder:
    """Stub ``load_registry`` as imported into the catalog module."""

    class _EmptyRegistry:
        @staticmethod
        def list_specs(*_a, **_k):
            return []

    rec = _Recorder(_EmptyRegistry())
    monkeypatch.setattr(catalog_mod, "load_registry", rec)
    return rec


def test_configured_servers_refuses_the_request_path(merged_config: _Recorder):
    """``SourceCatalog._configured_servers`` → ``get_merged_mcp_config``."""
    SourceCatalog._configured_servers(REQUEST_PATH)

    assert merged_config.only == {"cwd": REQUEST_PATH, "trust_cwd": False}


def test_workspace_mcp_config_refuses_the_request_path(merged_config: _Recorder):
    """``WorkspaceMcpConfig`` resolves the same request-supplied path."""
    WorkspaceMcpConfig.resolve_servers(["gitea"], project=REQUEST_PATH)

    assert merged_config.only == {"cwd": REQUEST_PATH, "trust_cwd": False}


def test_entries_builds_its_registry_untrusted(registry: _Recorder, merged_config: _Recorder):
    """``SourceCatalog.entries`` — reached from the sources route.

    Building the registry is what CONNECTS, and connecting is what spawns, so
    this is the site where a dropped kwarg would cost a process.
    """
    SourceCatalog.entries(REQUEST_PATH)

    assert registry.only == {"cwd": REQUEST_PATH, "trust_cwd": False}


def test_tools_for_builds_its_registry_untrusted(registry: _Recorder, merged_config: _Recorder):
    """``SourceCatalog.tools_for`` — the second registry build on this path."""
    SourceCatalog.tools_for(["gitea"], REQUEST_PATH)

    assert registry.only == {"cwd": REQUEST_PATH, "trust_cwd": False}


def test_a_positional_cwd_would_still_be_recorded(registry: _Recorder):
    """Guard on the guard: the recorder sees a positional ``cwd`` too.

    ``trust_cwd`` is keyword-only, but ``cwd`` need not be — so a recorder that
    only accepted keywords would raise rather than fail an assertion, and a
    reader could mistake that for a passing test.
    """
    registry("some/path", trust_cwd=False)

    assert registry.only == {"cwd": "some/path", "trust_cwd": False}
