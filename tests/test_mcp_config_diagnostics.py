"""Tests for MCP config-load diagnostics.

A syntactically broken MCP config file must not vanish silently — every
configured server disappearing with no log line naming the file, the parse
error, or even that the file existed. This suite pins:

- A malformed config produces a loud diagnostic naming the file + parse
  position (``TestMalformedConfigDiagnostic.test_names_file_and_parse_position``).
- The diagnostic is de-duplicated on the hot tool-call path while the file
  is unchanged, but re-fires the moment its content changes -- including a
  fix (confirmation) and a subsequent re-break (``TestMalformedConfigDiagnostic``).
- It is additive in ``MCPConnectionPool.status_snapshot()`` so ``/mcp`` can
  surface it (``test_surfaces_in_status_snapshot``).
- A single unreachable server in an otherwise-VALID config still degrades via
  the pool's existing classify/quarantine/backoff, which this diagnostic must
  leave untouched (``TestValidConfigUnaffected``).

``caplog`` captures nothing here -- these modules log through loguru, a
separate sink chain from stdlib ``logging`` (see tests/CLAUDE.md) -- so
assertions on log output add a temporary loguru sink instead.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import mewbo_tools.integration.mcp as mcp_mod
import pytest
from loguru import logger as _loguru
from mewbo_core.config import get_mcp_config_path
from mewbo_tools.integration.mcp import (
    MCPToolRunner,
    _load_mcp_config,
    get_last_config_error,
)
from mewbo_tools.integration.mcp_pool import (
    MCPConnectionPool,
    ServerState,
    get_mcp_pool,
)


@pytest.fixture(autouse=True)
def _reset_config_diagnostics():
    """Isolate the module-level dedup/last-error state between tests.

    ``_WARNED_CONFIG_HASHES``/``_LAST_CONFIG_ERROR`` are process-wide (by
    design -- the dedup must survive across many tool calls), so tests must
    not leak state into each other.
    """
    mcp_mod._WARNED_CONFIG_HASHES.clear()
    mcp_mod._LAST_CONFIG_ERROR = None
    yield
    mcp_mod._WARNED_CONFIG_HASHES.clear()
    mcp_mod._LAST_CONFIG_ERROR = None


def _capture_warnings() -> tuple[list[str], int]:
    """Add a temporary loguru sink capturing WARNING+ records as strings."""
    sink: list[str] = []
    handler_id = _loguru.add(lambda m: sink.append(str(m)), level="WARNING")
    return sink, handler_id


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class TestMalformedConfigDiagnostic:
    """`_load_mcp_config` must keep raising (its documented contract) while
    ALSO logging a loud, actionable, de-duplicated diagnostic."""

    def test_names_file_and_parse_position(self, tmp_path):
        bad = tmp_path / "mcp.json"
        bad.write_text('{"servers": {"grove": {"command": "x"}}}"\n')  # stray trailing quote

        sink, handler_id = _capture_warnings()
        try:
            with pytest.raises(json.JSONDecodeError):
                _load_mcp_config(path=str(bad))
        finally:
            _loguru.remove(handler_id)

        text = "".join(sink)
        assert str(bad) in text
        assert "line" in text.lower()
        assert "column" in text.lower()

    def test_does_not_repeat_while_file_unchanged(self, tmp_path):
        """The hot-path guard: the same broken file must warn exactly once,
        no matter how many times a tool call re-triggers the load."""
        bad = tmp_path / "mcp.json"
        bad.write_text('{"servers": {}"\n')

        sink, handler_id = _capture_warnings()
        try:
            for _ in range(5):
                with pytest.raises(json.JSONDecodeError):
                    _load_mcp_config(path=str(bad))
        finally:
            _loguru.remove(handler_id)

        assert len(sink) == 1

    def test_refires_after_file_content_changes(self, tmp_path):
        bad = tmp_path / "mcp.json"
        bad.write_text('{"servers": {}"\n')

        sink, handler_id = _capture_warnings()
        try:
            with pytest.raises(json.JSONDecodeError):
                _load_mcp_config(path=str(bad))
            # A different malformed edit must warn again -- not be
            # suppressed by the first failure's dedup entry.
            bad.write_text("{\"servers\": {\"a\": 1,}\n")
            with pytest.raises(json.JSONDecodeError):
                _load_mcp_config(path=str(bad))
        finally:
            _loguru.remove(handler_id)

        assert len(sink) == 2

    def test_surfaces_in_status_snapshot(self, tmp_path):
        bad = tmp_path / "mcp.json"
        bad.write_text('{"servers": {}"\n')

        with pytest.raises(json.JSONDecodeError):
            _load_mcp_config(path=str(bad))

        snap = get_mcp_pool().status_snapshot()
        assert snap["_config_error"]["status"] == "error"
        assert str(bad) in snap["_config_error"]["reason"]

    def test_clears_on_fix_and_rearms_for_a_repeat_break(self, tmp_path):
        """Fixing the file must both stop the snapshot noise (confirmation)
        AND re-arm the warning, so a later identical re-break is not
        silently swallowed by a stale dedup entry."""
        bad = tmp_path / "mcp.json"
        bad.write_text('{"servers": {}"\n')

        sink, handler_id = _capture_warnings()
        try:
            with pytest.raises(json.JSONDecodeError):
                _load_mcp_config(path=str(bad))
            assert get_last_config_error() is not None

            bad.write_text('{"servers": {}}\n')
            _load_mcp_config(path=str(bad))
            assert get_last_config_error() is None
            assert "_config_error" not in get_mcp_pool().status_snapshot()

            # Same broken content as the very first write.
            bad.write_text('{"servers": {}"\n')
            with pytest.raises(json.JSONDecodeError):
                _load_mcp_config(path=str(bad))
        finally:
            _loguru.remove(handler_id)

        assert len(sink) == 2


class TestToolRunnerConfigVsTransportFailure:
    """The pool-invocation broad ``except Exception`` must not mask a
    config-file error by silently retrying via the legacy client -- that
    would just re-parse the identical broken file for no benefit."""

    def test_broken_config_raises_without_legacy_fallback(self, tmp_path, monkeypatch):
        config_path = get_mcp_config_path()
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write('{"servers": {}"\n')

        # Isolate subtree/CWD discovery to an empty dir -- otherwise merged
        # discovery would pick up this checkout's own real .mcp.json and
        # paper over the broken global layer, same as it would for any user
        # with a valid project-local config alongside a broken global one.
        runner = MCPToolRunner(server_name="grove", tool_name="ask", cwd=str(tmp_path))

        legacy_calls = {"n": 0}

        async def _legacy(self, payload):
            legacy_calls["n"] += 1
            raise AssertionError("legacy fallback must not run for a config error")

        monkeypatch.setattr(MCPToolRunner, "_invoke_legacy", _legacy)

        with pytest.raises(json.JSONDecodeError):
            asyncio.run(runner._invoke_async({"q": "x"}))
        assert legacy_calls["n"] == 0


class TestValidConfigUnaffected:
    """A syntactically VALID config must show no new noise and no behavior
    change -- the pool's existing per-server classify/quarantine/backoff
    (deliberate design) is untouched."""

    def test_one_unreachable_server_still_degrades_via_backoff(self):
        pool = MCPConnectionPool()
        cfg = {
            "servers": {
                "good": {"command": "good-mcp"},
                "bad": {"command": "bad-mcp"},
            }
        }

        async def _connect(name, server_cfg):
            if name == "bad":
                raise ConnectionRefusedError("Connection refused")
            return ServerState(name=name, config=server_cfg, connected=True, tools=[])

        sink, handler_id = _capture_warnings()
        try:
            with patch.object(pool, "_connect_single", side_effect=_connect):
                asyncio.run(pool.connect_all(cfg))
        finally:
            _loguru.remove(handler_id)

        snap = pool.status_snapshot()
        assert snap["good"]["status"] == "connected"
        assert snap["bad"]["status"] == "backoff"
        assert "_config_error" not in snap
        assert get_last_config_error() is None
        assert not any("not valid JSON" in line for line in sink)

    def test_valid_config_loads_all_servers(self):
        pool = MCPConnectionPool()
        cfg = {
            "servers": {
                "a": {"command": "a-mcp"},
                "b": {"command": "b-mcp"},
            }
        }

        async def _connect(name, server_cfg):
            return ServerState(
                name=name,
                config=server_cfg,
                connected=True,
                tools=[_FakeTool(f"{name}_tool")],
            )

        with patch.object(pool, "_connect_single", side_effect=_connect):
            results = asyncio.run(pool.connect_all(cfg))

        assert set(results.keys()) == {"a", "b"}
        assert results["a"] == ["a_tool"]
        assert results["b"] == ["b_tool"]
        snap = pool.status_snapshot()
        assert snap["a"]["status"] == "connected"
        assert snap["b"]["status"] == "connected"
        assert "_config_error" not in snap
