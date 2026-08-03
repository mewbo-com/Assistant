"""Tests for tool partitioning, timeout, and ToolSpec typed fields."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mewbo_core.agents.spawn_agent import AgentError
from mewbo_core.classes import ToolResult
from mewbo_core.loop.tool_use_loop import ToolBatch, ToolUseLoop
from mewbo_core.tooling.tool_registry import ToolSpec

# -- Helpers ----------------------------------------------------------------


def _spec(tid: str, concurrent: bool = True, timeout: float = 120.0) -> ToolSpec:
    return ToolSpec(
        tool_id=tid,
        name=tid,
        description=f"t-{tid}",
        factory=lambda: None,
        concurrency_safe=concurrent,
        timeout=timeout,
    )


def _tc(name: str) -> dict:
    return {"name": name, "id": f"call_{name}", "args": {}}


def _loop(session_tools: list | None = None) -> ToolUseLoop:
    loop = object.__new__(ToolUseLoop)
    loop._tool_registry = MagicMock()
    loop._tool_registry.get_spec = MagicMock(return_value=None)
    loop._session_tools = session_tools or []
    return loop


class _FakeSessionTool:
    """A session tool with no ``execution_timeout`` hook at all — the
    byte-identical control (undeclared tools are untouched)."""

    def __init__(self, tool_id: str):
        self.tool_id = tool_id


class _DeclaringSessionTool:
    """A session tool that declares its own execution ceiling via the
    ``getattr`` convention ``_declared_execution_timeout`` reads."""

    def __init__(self, tool_id: str, value):
        self.tool_id = tool_id
        self._value = value

    def execution_timeout(self, tool_input):
        return self._value


class _RaisingSessionTool:
    """A session tool whose ``execution_timeout`` hook raises — must not
    propagate; the call falls back to the flat ceiling instead."""

    def __init__(self, tool_id: str):
        self.tool_id = tool_id

    def execution_timeout(self, tool_input):
        raise RuntimeError("ceiling hook exploded")


# -- ToolBatch --------------------------------------------------------------


class TestToolBatch:
    def test_fields(self):
        b = ToolBatch(calls=[_tc("a")], concurrent=True)
        assert b.concurrent and len(b.calls) == 1


# -- _partition_tool_calls --------------------------------------------------


class TestPartitionToolCalls:
    def test_all_concurrent(self):
        specs = {n: _spec(n) for n in "abc"}
        batches = _loop()._partition_tool_calls([_tc(n) for n in "abc"], specs)
        assert len(batches) == 1
        assert batches[0].concurrent and len(batches[0].calls) == 3

    def test_all_exclusive(self):
        specs = {n: _spec(n, concurrent=False) for n in "ab"}
        batches = _loop()._partition_tool_calls([_tc("a"), _tc("b")], specs)
        assert len(batches) == 2
        assert all(not b.concurrent for b in batches)

    def test_mixed(self):
        specs = {"a": _spec("a"), "b": _spec("b", concurrent=False), "c": _spec("c")}
        batches = _loop()._partition_tool_calls([_tc("a"), _tc("b"), _tc("c")], specs)
        assert len(batches) == 3
        assert batches[0].concurrent and not batches[1].concurrent and batches[2].concurrent

    def test_consecutive_merged(self):
        specs = {
            "a": _spec("a"),
            "b": _spec("b"),
            "c": _spec("c", concurrent=False),
            "d": _spec("d"),
            "e": _spec("e"),
        }
        batches = _loop()._partition_tool_calls([_tc(n) for n in "abcde"], specs)
        assert len(batches) == 3
        assert len(batches[0].calls) == 2
        assert len(batches[2].calls) == 2

    def test_unknown_defaults_safe(self):
        batches = _loop()._partition_tool_calls([_tc("x")], {})
        assert batches[0].concurrent

    def test_single(self):
        batches = _loop()._partition_tool_calls([_tc("a")], {"a": _spec("a")})
        assert len(batches) == 1

    def test_empty(self):
        assert _loop()._partition_tool_calls([], {}) == []


# -- _get_tool_timeout ------------------------------------------------------


class TestGetToolTimeout:
    def test_default(self):
        assert _loop()._get_tool_timeout("any") == 120.0

    def test_from_spec(self):
        loop = _loop()
        loop._tool_registry.get_spec.return_value = _spec("t", timeout=30.0)
        assert loop._get_tool_timeout("t") == 30.0

    def test_no_registry(self):
        loop = _loop()
        loop._tool_registry = None
        assert loop._get_tool_timeout("t") == 120.0


# -- _declared_execution_timeout ---------------------------------------------


class TestDeclaredExecutionTimeout:
    """(declared?, seconds) for one call — the boolean is what lets ``None``
    mean "no ceiling at all" while still distinguishing that from a tool
    that declined to declare anything."""

    def test_unknown_tool_reports_not_declared(self):
        loop = _loop()
        assert loop._declared_execution_timeout("nope", {}) == (False, None)

    def test_tool_with_no_hook_reports_not_declared(self):
        loop = _loop([_FakeSessionTool("plain")])
        assert loop._declared_execution_timeout("plain", {}) == (False, None)

    def test_hook_returning_none_is_a_declared_unbounded_ceiling(self):
        loop = _loop([_DeclaringSessionTool("ask", None)])
        assert loop._declared_execution_timeout("ask", {}) == (True, None)

    def test_hook_returning_a_number_is_declared(self):
        loop = _loop([_DeclaringSessionTool("ask", 45)])
        assert loop._declared_execution_timeout("ask", {}) == (True, 45.0)

    @pytest.mark.parametrize("garbage", ["soon", True, False, object()])
    def test_hook_returning_non_numeric_falls_back_to_not_declared(self, garbage):
        loop = _loop([_DeclaringSessionTool("ask", garbage)])
        assert loop._declared_execution_timeout("ask", {}) == (False, None)

    def test_raising_hook_falls_back_to_not_declared(self):
        loop = _loop([_RaisingSessionTool("ask")])
        assert loop._declared_execution_timeout("ask", {}) == (False, None)


# -- _tool_execution_timeout --------------------------------------------------


class TestToolExecutionTimeout:
    """A session tool's own declaration wins outright; failing that, the
    check_agents wait/timeout arm still wins over the flat ceiling; failing
    that, the flat ceiling is unchanged — the same three-rung order as
    before this tool learned to declare anything.
    """

    def test_undeclared_non_wait_tool_is_byte_identical_to_before(self):
        loop = _loop()
        loop._tool_registry.get_spec.return_value = _spec("t", timeout=45.0)
        assert loop._tool_execution_timeout("t", _tc("t")) == 45.0

    def test_undeclared_check_agents_wait_arm_still_wins(self):
        loop = _loop()
        tc = {"name": "check_agents", "args": {"wait": True, "timeout": 300}, "id": "c1"}
        assert loop._tool_execution_timeout("check_agents", tc) == 330.0

    def test_declared_none_removes_the_ceiling_entirely(self):
        loop = _loop([_DeclaringSessionTool("ask", None)])
        assert loop._tool_execution_timeout("ask", _tc("ask")) is None

    def test_declared_number_wins_over_a_larger_registry_ceiling(self):
        loop = _loop([_DeclaringSessionTool("ask", 45)])
        loop._tool_registry.get_spec.return_value = _spec("ask", timeout=300.0)
        assert loop._tool_execution_timeout("ask", _tc("ask")) == 45.0

    def test_declared_garbage_falls_back_to_the_flat_ceiling(self):
        loop = _loop([_DeclaringSessionTool("ask", "soon")])
        assert loop._tool_execution_timeout("ask", _tc("ask")) == 120.0

    def test_declared_bool_falls_back_to_the_flat_ceiling(self):
        loop = _loop([_DeclaringSessionTool("ask", True)])
        assert loop._tool_execution_timeout("ask", _tc("ask")) == 120.0

    def test_raising_hook_falls_back_and_does_not_propagate(self):
        loop = _loop([_RaisingSessionTool("ask")])
        assert loop._tool_execution_timeout("ask", _tc("ask")) == 120.0


# -- ToolSpec typed fields --------------------------------------------------


class TestToolSpecFields:
    def test_defaults(self):
        s = ToolSpec(tool_id="t", name="t", description="t", factory=lambda: None)
        assert s.concurrency_safe is True
        assert s.read_only is False
        assert s.interrupt_behavior == "block"
        assert s.max_result_chars == 2000
        assert s.timeout == 120.0

    def test_custom(self):
        s = ToolSpec(
            tool_id="t",
            name="t",
            description="t",
            factory=lambda: None,
            concurrency_safe=False,
            read_only=True,
            interrupt_behavior="cancel",
            max_result_chars=500,
            timeout=30.0,
        )
        assert not s.concurrency_safe and s.read_only
        assert s.interrupt_behavior == "cancel"
        assert s.max_result_chars == 500 and s.timeout == 30.0


# -- ToolResult -------------------------------------------------------------


class TestToolResult:
    def test_defaults(self):
        r = ToolResult(content="hi")
        assert r.success and r.error is None and not r.truncated

    def test_error(self):
        r = ToolResult(content="", success=False, error="timeout")
        assert not r.success and r.error == "timeout"

    def test_truncated(self):
        r = ToolResult(content="x", truncated=True, original_length=9999)
        assert r.truncated and r.original_length == 9999


# -- AgentError -------------------------------------------------------------


class TestAgentError:
    def test_str_basic(self):
        e = AgentError(agent_id="abc", depth=2, task="do stuff", error="timeout", steps_completed=5)
        s = str(e)
        assert "abc" in s and "depth=2" in s and "5 steps" in s and "timeout" in s

    def test_str_with_tool(self):
        e = AgentError(
            agent_id="x",
            depth=1,
            task="t",
            error="fail",
            last_tool="shell",
            steps_completed=3,
        )
        assert "shell" in str(e)

    def test_str_no_tool(self):
        e = AgentError(agent_id="x", depth=0, task="t", error="e")
        assert "at tool" not in str(e)

    def test_defaults(self):
        e = AgentError(agent_id="a", depth=0, task="t", error="e")
        assert e.last_tool is None and e.steps_completed == 0
