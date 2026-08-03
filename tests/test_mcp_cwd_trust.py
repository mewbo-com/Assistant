#!/usr/bin/env python3
"""A working directory's own ``.mcp.json`` is admitted only when it is trusted.

An MCP server entry is a ``command`` this process spawns, and the spawn happens
inside CONFIG RESOLUTION — before any tool allowlist is consulted. So the
property under test is not "the hostile tool is not callable"; it is "the
hostile entry never reaches the merged config, and no process runs".

Both resolution legs are exercised, because they happen at different times: the
registry is built once, up front, while an ``MCPToolRunner`` re-resolves the
same ``cwd`` at INVOCATION time — long enough after the build for a directory
that was empty then to hold a repository now.

Isolation: the global MCP config is pinned to a temp file, the untrusted-root
registry is cleared around every test, and the only process any of this can
spawn is an inert ``sh -c 'echo … > marker'`` that touches no network.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from mewbo_core.config import (
    get_merged_mcp_config,
    is_untrusted_cwd,
    register_untrusted_cwd,
    reset_config,
    set_mcp_config_path,
    unregister_untrusted_cwd,
)
from mewbo_core.tooling.tool_registry import load_registry, reset_registry_cache
from mewbo_tools.integration.mcp import MCPToolRunner, _load_mcp_config, _normalize_mcp_config

HOSTILE_SERVER = "repo_planted"
OPERATOR_SERVER = "opsrv"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _hostile_entry(marker: Path) -> dict:
    """An inert server whose spawn is observable: it writes one file and exits.

    It speaks no MCP, so the handshake always fails — which is the point. The
    marker records that the process RAN regardless of whether the connection
    succeeded, and that is the fact a tool allowlist cannot gate.
    """
    return {
        "command": "sh",
        "args": ["-c", f"echo SPAWNED > {marker}"],
    }


@pytest.fixture
def operator_config(tmp_path: Path) -> Path:
    """Pin the global (operator-owned) MCP config to a temp file."""
    global_path = tmp_path / "operator_mcp.json"
    _write_json(
        global_path,
        {
            "servers": {
                OPERATOR_SERVER: {
                    "command": "operator-binary",
                    "args": ["--from=operator"],
                    "env": {"OPERATOR_TOKEN": "operator-secret"},
                }
            }
        },
    )
    set_mcp_config_path(str(global_path))
    yield global_path
    reset_config()


@pytest.fixture(autouse=True)
def _clean_untrusted_roots():
    """No registration may leak between tests — the gate is process-wide."""
    from mewbo_core.config import _UNTRUSTED_CWDS

    _UNTRUSTED_CWDS.clear()
    yield
    _UNTRUSTED_CWDS.clear()
    reset_registry_cache()


class TestMergedConfigTrustBoundary:
    """``get_merged_mcp_config`` — the one seam every resolution goes through."""

    def test_trusted_cwd_still_contributes(self, tmp_path: Path, operator_config: Path):
        """Backwards compatibility: an ordinary project's .mcp.json still lands.

        This is the guard against over-fixing. A developer's own checkout
        contributing MCP servers is a feature, and the default must keep it.
        """
        project = tmp_path / "my-project"
        _write_json(project / ".mcp.json", {"servers": {HOSTILE_SERVER: {"command": "x"}}})

        servers = get_merged_mcp_config(cwd=str(project)).get("servers", {})

        assert HOSTILE_SERVER in servers
        assert servers[HOSTILE_SERVER]["command"] == "x"
        assert OPERATOR_SERVER in servers

    def test_untrusted_by_argument_excludes_cwd_tier(self, tmp_path: Path, operator_config: Path):
        """``trust_cwd=False`` drops the directory's own ``.mcp.json`` entirely."""
        clone = tmp_path / "clone"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: {"command": "x"}}})

        servers = get_merged_mcp_config(cwd=str(clone), trust_cwd=False).get("servers", {})

        assert HOSTILE_SERVER not in servers
        # The operator's own config is untouched — only the directory tier goes.
        assert servers[OPERATOR_SERVER]["command"] == "operator-binary"

    def test_untrusted_by_registration_excludes_cwd_tier(
        self, tmp_path: Path, operator_config: Path
    ):
        """A registered root excludes the tier without any caller threading a flag.

        This is what covers the resolution legs that cannot pass an argument —
        a caller deep under someone else's API, or one that never learned the
        directory was untrusted.
        """
        clone_root = tmp_path / "clones"
        clone = clone_root / "job-1"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: {"command": "x"}}})
        register_untrusted_cwd(clone_root)
        try:
            # Note the default argument: the caller asked for the trusted path.
            servers = get_merged_mcp_config(cwd=str(clone)).get("servers", {})
            assert HOSTILE_SERVER not in servers
            assert is_untrusted_cwd(str(clone)) is True
        finally:
            unregister_untrusted_cwd(clone_root)

        # Unregistering restores the ordinary behaviour — the gate is not sticky.
        assert HOSTILE_SERVER in get_merged_mcp_config(cwd=str(clone)).get("servers", {})

    def test_subtree_variant_is_excluded(self, tmp_path: Path, operator_config: Path):
        """The surface is any directory in the tree, not just its root.

        One file in one subdirectory is enough — which is what makes this
        reachable from a single contributed commit rather than a fork.
        """
        clone = tmp_path / "clone"
        _write_json(
            clone / "packages" / "sub" / ".mcp.json",
            {"servers": {HOSTILE_SERVER: {"command": "x"}}},
        )
        assert not (clone / ".mcp.json").exists()

        # Positive control: trusted, the subtree walk DOES find it. Without
        # this the exclusion below could pass because the walk never ran.
        assert HOSTILE_SERVER in get_merged_mcp_config(cwd=str(clone)).get("servers", {})

        servers = get_merged_mcp_config(cwd=str(clone), trust_cwd=False).get("servers", {})
        assert HOSTILE_SERVER not in servers

    def test_deep_merge_env_injection_is_excluded(self, tmp_path: Path, operator_config: Path):
        """A repo file naming only ``env`` must not ride inside an operator server.

        The merge recurses, so out-prioritising the tier is not enough: a file
        that names no ``command`` keeps the operator's binary AND its secret
        while adding a variable of its own. Excluding the tier is what closes
        it, and this test is what proves the fix was exclusion, not ordering.
        """
        clone = tmp_path / "clone"
        _write_json(
            clone / ".mcp.json",
            {"servers": {OPERATOR_SERVER: {"env": {"INJECTED_BY_REPO": "yes"}}}},
        )

        # Positive control: trusted, the injection lands — the operator's own
        # command and token survive alongside the attacker's variable.
        trusted = get_merged_mcp_config(cwd=str(clone))["servers"][OPERATOR_SERVER]
        assert trusted["command"] == "operator-binary"
        assert trusted["env"]["INJECTED_BY_REPO"] == "yes"

        untrusted = get_merged_mcp_config(cwd=str(clone), trust_cwd=False)["servers"][
            OPERATOR_SERVER
        ]
        assert untrusted["command"] == "operator-binary"
        assert untrusted["env"] == {"OPERATOR_TOKEN": "operator-secret"}
        assert "INJECTED_BY_REPO" not in untrusted["env"]


class TestBothResolutionLegs:
    """Registry build (once, up front) and runner re-resolution (per call)."""

    def test_runner_reresolution_excludes_untrusted_cwd(
        self, tmp_path: Path, operator_config: Path
    ):
        """Leg 2 — the config a runner re-reads at invocation time.

        Calls exactly what ``MCPToolRunner`` calls (``_load_mcp_config`` with
        the runner's own ``cwd``), with the directory populated AFTER the
        registry would have been built.
        """
        clone = tmp_path / "clone"
        clone.mkdir()
        runner = MCPToolRunner(server_name=HOSTILE_SERVER, tool_name="t", cwd=str(clone))

        # The clone lands only now — this is the window the ordering argument
        # never covered.
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: {"command": "x"}}})
        register_untrusted_cwd(clone)

        config = _load_mcp_config(cwd=runner._cwd, trust_cwd=runner._trust_cwd)

        assert HOSTILE_SERVER not in config.get("servers", {})

    def test_runner_carries_an_explicit_untrusted_flag(self, tmp_path: Path, operator_config: Path):
        """A runner built for an untrusted scope keeps that decision on itself."""
        clone = tmp_path / "clone"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: {"command": "x"}}})
        runner = MCPToolRunner(
            server_name=HOSTILE_SERVER, tool_name="t", cwd=str(clone), trust_cwd=False
        )

        config = _load_mcp_config(cwd=runner._cwd, trust_cwd=runner._trust_cwd)

        assert HOSTILE_SERVER not in config.get("servers", {})

    def test_registry_hands_the_decision_to_its_runners(
        self, tmp_path: Path, operator_config: Path
    ):
        """``load_registry(trust_cwd=False)`` must not build trusting runners.

        A registry that excluded the tier at build time but handed its runners
        ``trust_cwd=True`` would hold the boundary for discovery and leak at
        call time — the exact split this issue is about. Driven from an
        explicit manifest so an MCP spec definitely EXISTS to inspect;
        asserting over a spec list that discovery left empty would pass
        without testing anything.
        """
        clone = tmp_path / "clone"
        clone.mkdir()
        manifest = tmp_path / "manifest.json"
        _write_json(
            manifest,
            {
                "tools": [
                    {
                        "tool_id": "mcp_probe",
                        "name": "probe",
                        "kind": "mcp",
                        "server": HOSTILE_SERVER,
                        "tool": "probe",
                        "enabled": True,
                    }
                ]
            },
        )

        mcp_specs = [
            s
            for s in load_registry(str(manifest), cwd=str(clone), trust_cwd=False).list_specs()
            if s.kind == "mcp"
        ]

        assert mcp_specs, "manifest produced no MCP spec — the assertion below would be vacuous"
        for spec in mcp_specs:
            assert spec.factory()._trust_cwd is False
        # And the default keeps building trusting runners.
        trusting = [
            s for s in load_registry(str(manifest), cwd=str(clone)).list_specs() if s.kind == "mcp"
        ]
        assert trusting and all(s.factory()._trust_cwd for s in trusting)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell for the spawn probe")
class TestNoProcessIsSpawned:
    """The load-bearing one: resolution CONNECTS, and connecting SPAWNS.

    ``filter_specs`` is a different layer and is never consulted on this path,
    so an allowlist can never be the mitigation. The only assertion that means
    anything is that the process did not run.
    """

    @staticmethod
    def _refresh(config: dict) -> None:
        pytest.importorskip("langchain_mcp_adapters")
        from mewbo_tools.integration.mcp_pool import get_mcp_pool, reset_mcp_pool

        reset_mcp_pool()
        pool = get_mcp_pool()
        try:
            # The handshake fails (the probe speaks no MCP); the spawn is what
            # we are measuring, so the failure is swallowed deliberately.
            asyncio.run(pool.refresh_if_config_changed(config, connect=True))
        except Exception:
            pass
        finally:
            reset_mcp_pool()

    def test_trusted_cwd_does_spawn_the_probe(self, tmp_path: Path, operator_config: Path):
        """Positive control: without the fix's exclusion, the marker appears.

        Without this the negative test below could pass because the probe is
        broken rather than because anything was excluded.
        """
        clone = tmp_path / "clone"
        marker = tmp_path / "spawned_trusted.marker"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: _hostile_entry(marker)}})
        set_mcp_config_path(str(tmp_path / "absent_operator.json"))

        config = _normalize_mcp_config(_load_mcp_config(cwd=str(clone)))
        assert HOSTILE_SERVER in config["servers"]
        self._refresh(config)

        assert marker.exists(), "probe did not spawn — the negative test would be vacuous"

    def test_untrusted_cwd_spawns_nothing(self, tmp_path: Path, operator_config: Path):
        """The property: no marker, because no process ran."""
        clone = tmp_path / "clone"
        marker = tmp_path / "spawned_untrusted.marker"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: _hostile_entry(marker)}})
        register_untrusted_cwd(clone)

        config = _normalize_mcp_config(_load_mcp_config(cwd=str(clone)))
        assert HOSTILE_SERVER not in config.get("servers", {})
        self._refresh(config)

        assert not marker.exists()

    def test_registry_build_spawns_from_a_trusted_cwd(self, tmp_path: Path, operator_config: Path):
        """Leg 1, positive control: ``load_registry`` alone runs the probe.

        Discovery connects, so building a registry against a populated
        directory is by itself enough to spawn every command it names. This is
        the fact the "it is built before the clone lands" argument rested on.
        """
        pytest.importorskip("langchain_mcp_adapters")
        clone = tmp_path / "clone"
        marker = tmp_path / "registry_trusted.marker"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: _hostile_entry(marker)}})
        set_mcp_config_path(str(tmp_path / "absent_operator.json"))
        reset_registry_cache()

        load_registry(cwd=str(clone))

        assert marker.exists(), "registry build did not spawn — the next test would be vacuous"

    def test_registry_build_from_untrusted_cwd_spawns_nothing(
        self, tmp_path: Path, operator_config: Path
    ):
        """Leg 1, the property: nothing runs, and no spec is admitted."""
        pytest.importorskip("langchain_mcp_adapters")
        clone = tmp_path / "clone"
        marker = tmp_path / "registry_untrusted.marker"
        _write_json(clone / ".mcp.json", {"servers": {HOSTILE_SERVER: _hostile_entry(marker)}})
        set_mcp_config_path(str(tmp_path / "absent_operator.json"))
        register_untrusted_cwd(clone)
        reset_registry_cache()

        registry = load_registry(cwd=str(clone))

        assert not marker.exists()
        assert not [s for s in registry.list_specs() if s.kind == "mcp"]

    def test_untrusted_subtree_spawns_nothing(self, tmp_path: Path, operator_config: Path):
        """Same, for a ``.mcp.json`` one directory down."""
        clone = tmp_path / "clone"
        marker = tmp_path / "spawned_subtree.marker"
        _write_json(
            clone / "tools" / ".mcp.json",
            {"servers": {HOSTILE_SERVER: _hostile_entry(marker)}},
        )
        register_untrusted_cwd(clone)

        config = _normalize_mcp_config(_load_mcp_config(cwd=str(clone)))
        assert HOSTILE_SERVER not in config.get("servers", {})
        self._refresh(config)

        assert not marker.exists()
