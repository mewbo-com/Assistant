"""A configured MCP server can be switched off without deleting its entry.

Removing a server from the config to turn it off throws away its URL, headers
and credentials. ``enabled: false`` keeps the entry and takes the server out of
the active set.

Two properties carry the whole feature and both are easy to get wrong:
absent-means-enabled (so no existing config changes behaviour), and the flag
never reaching the adapter — a stray key is spread into the session constructor
and raises ``unexpected keyword argument``, so a switch implemented carelessly
breaks the very server it was meant to control.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mewbo_tools.integration.mcp import (
    _load_mcp_config,
    _normalize_mcp_config,
    disabled_servers,
)
from mewbo_tools.integration.mcp_pool import MCPConnectionPool, ServerState


def _cfg(**servers: dict) -> dict:
    return {"mcpServers": servers}


def _write(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _clean_disabled_registry():
    # A direct clear, not a re-normalize-with-empty-servers trick:
    # `_normalize_mcp_config` must NOT blind-clear `_DISABLED_SERVERS` on
    # every call: that erases a disabled name across the double-normalize
    # sequence `MCPToolRunner._invoke_via_pool` -> `refresh_if_config_changed`
    # triggers on every tool call (see TestDoubleNormalizeDoesNotForget below).
    # An empty ``servers`` dict therefore sees nothing and clears nothing, so
    # tests must reach the registry directly to isolate from each other.
    import mewbo_tools.integration.mcp as mcp_mod

    mcp_mod._DISABLED_SERVERS.clear()
    yield
    mcp_mod._DISABLED_SERVERS.clear()


class TestAbsentMeansEnabled:
    """The migration guard. Every existing config must be untouched."""

    def test_no_flag_keeps_the_server(self, tmp_path: Path):
        path = _write(tmp_path, _cfg(alpha={"url": "http://a/mcp"}))
        assert "alpha" in _load_mcp_config(path)["servers"]
        assert disabled_servers() == frozenset()

    def test_no_flag_leaves_the_config_byte_identical(self):
        without = _normalize_mcp_config(_cfg(alpha={"url": "http://a/mcp"}))
        with_true = _normalize_mcp_config(_cfg(alpha={"url": "http://a/mcp", "enabled": True}))
        assert without == with_true, "enabled:true must equal the absent case exactly"


class TestDisabling:
    def test_disabled_server_leaves_the_active_set(self, tmp_path: Path):
        path = _write(
            tmp_path,
            _cfg(alpha={"url": "http://a/mcp"}, beta={"url": "http://b/mcp", "enabled": False}),
        )
        servers = _load_mcp_config(path)["servers"]
        assert set(servers) == {"alpha"}, "a disabled server must not reach any consumer"

    def test_disabled_server_is_still_reported(self, tmp_path: Path):
        _load_mcp_config(_write(tmp_path, _cfg(beta={"url": "http://b/mcp", "enabled": False})))
        assert disabled_servers() == frozenset({"beta"})

    def test_only_a_literal_false_disables(self):
        # A typo must leave the server ON. Silently removing a server because
        # someone wrote "false" instead of false is the failure mode this whole
        # area is being hardened against.
        for typo in ("false", 0, None, ""):
            cfg = _normalize_mcp_config(_cfg(alpha={"url": "http://a/mcp", "enabled": typo}))
            assert "alpha" in cfg["servers"], f"{typo!r} must not disable a server"


class TestFlagNeverReachesTheAdapter:
    """The `_UNSUPPORTED_ADAPTER_KEYS` trap, in its own right.

    langchain-mcp-adapters spreads the connection dict into its session
    factory, so any key it does not accept raises ``unexpected keyword
    argument`` at connect time — long after config load, and pointing at the
    wrong thing.
    """

    def test_enabled_is_stripped_from_a_kept_server(self):
        cfg = _normalize_mcp_config(_cfg(alpha={"url": "http://a/mcp", "enabled": True}))
        assert "enabled" not in cfg["servers"]["alpha"]

    def test_no_key_survives_that_the_adapter_would_reject(self):
        cfg = _normalize_mcp_config(
            _cfg(alpha={"url": "http://a/mcp", "enabled": True, "type": "http"})
        )
        assert set(cfg["servers"]["alpha"]) == {"url", "transport"}


class TestNeverDialledViaThePool:
    """The no-dial property, proven directly against the real connect path --
    not inferred from the absence of tools. ``_connect_single`` is the ONE
    place a config is spread into the adapter's session constructor; a spy
    there proves a disabled server is never even attempted."""

    def test_disabled_server_is_never_connected(self, monkeypatch):
        dialed: list[str] = []

        async def _fake_connect_single(self, name, cfg):
            dialed.append(name)
            return ServerState(name=name, config=cfg, connected=True, tools=[])

        monkeypatch.setattr(MCPConnectionPool, "_connect_single", _fake_connect_single)

        pool = MCPConnectionPool()
        cfg = _cfg(alpha={"command": "a"}, beta={"command": "b", "enabled": False})
        results = asyncio.run(pool.connect_all(cfg))

        assert dialed == ["alpha"], "the disabled server must never be dialled"
        assert "beta" not in results, "a disabled server contributes zero tool schemas"
        assert results["alpha"] == []


class TestDoubleNormalizeDoesNotForget:
    """A config gets normalized more than once on the SAME real path
    (``MCPToolRunner._invoke_via_pool`` normalizes, then hands the result to
    ``MCPConnectionPool.refresh_if_config_changed``, which normalizes again).
    A disabled server is already gone from ``servers`` by the second pass, so
    a normalize that blindly cleared its bookkeeping on every call would
    un-disable it there -- silently dropping it from ``status_snapshot()``
    even though the config never changed."""

    def test_status_snapshot_survives_a_second_normalize_pass(self):
        raw = _cfg(alpha={"command": "a"}, beta={"command": "b", "enabled": False})

        # First pass: mirrors `_invoke_via_pool`'s own normalize call.
        once = _normalize_mcp_config(raw)
        assert "beta" not in once["servers"]

        # Second pass: mirrors `refresh_if_config_changed`'s internal
        # re-normalize of the ALREADY-normalized config.
        pool = MCPConnectionPool()
        asyncio.run(pool.refresh_if_config_changed(once, connect=False))

        snap = pool.status_snapshot()
        assert snap["beta"]["status"] == "disabled", (
            "a second normalize pass over an already-stripped config must not "
            "erase the server's disabled bookkeeping"
        )
        assert "beta" not in pool._mcp_config.get("servers", {})
