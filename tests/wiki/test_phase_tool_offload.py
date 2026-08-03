"""``wiki_clone_repo`` and ``wiki_scan_tree`` must not own the event loop either.

Same defect as the graph build, same two halves. A clone is a git subprocess
whose credential chain alone can legitimately run for 1500s (five candidates at
the 300s per-attempt cap in ``clone.py``); a scan reads and hashes every file it
keeps. Both were ``async def handle`` bodies with no ``await`` in them, so both
parked the loop for their whole duration — and both would have been killed at
the registry's ToolSpec-less 120s the moment they moved off it, which is why
each declares its own ceiling in the same change.

The blocking body of each tool is stubbed here on purpose: what these cover is
the wrapper, and the bodies are exercised unchanged by the existing clone/scan
suites.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.common import MockSpeaker
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_graph.plugins.wiki import clone as clone_mod, scan as scan_mod
from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
from mewbo_graph.plugins.wiki.scan import WikiScanTreeTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

_SAFETY_S = 3.0
_MAX_TURNS = 400

# (tool class, its module, the config key it reads, its default constant)
_PHASE_TOOLS = [
    pytest.param(WikiCloneRepoTool, clone_mod, "clone_s", id="clone"),
    pytest.param(WikiScanTreeTool, scan_mod, "scan_s", id="scan"),
]


@pytest.fixture
def job(tmp_path, monkeypatch):
    """A resolvable indexing-job ctx: store + attached session + clone dir."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store.create_job(
        IndexingJob(
            jobId="j1",
            slug="x/y",
            status="scanning",
            scannedCount=0,
            totalCount=0,
            currentFile=None,
        )
    )
    store.attach_job_session("j1", "sess-1")
    (tmp_path / "clones" / "j1").mkdir(parents=True)
    return store, "sess-1"


def _budget_config(key: str, seconds: float):
    """A ``get_config_value`` stand-in that injects one phase budget."""
    def _fake(*keys, default=None):
        return seconds if keys == ("wiki", "phase_timeouts", key) else default

    return _fake


@pytest.mark.parametrize(("tool_cls", "module", "key"), _PHASE_TOOLS)
def test_phase_tool_runs_off_the_loop(job, tool_cls, module, key):
    """A concurrent coroutine must make progress while the phase body runs.

    The handshake is the assertion: the worker blocks until ``release``, and only
    the event loop can set it.
    """
    store, session_id = job
    entered = threading.Event()
    release = threading.Event()
    seen: dict[str, object] = {}

    def _blocking_body(self, action_step):
        seen["worker_thread"] = threading.get_ident()
        entered.set()
        seen["released"] = release.wait(timeout=_SAFETY_S)
        return MockSpeaker(content="{'ok': True}")

    async def _drive():
        seen["loop_thread"] = threading.get_ident()
        tool = tool_cls(session_id=session_id)
        task = asyncio.create_task(tool.handle(MagicMock(tool_input={})))
        turns = 0
        while not entered.is_set() and turns < _MAX_TURNS:
            await asyncio.sleep(0.005)
            turns += 1
        release.set()
        return turns, await task

    with (
        patch.object(module, "_resolve_runtime", return_value=MagicMock(wiki_store=store)),
        patch.object(tool_cls, "_handle_blocking", _blocking_body),
    ):
        turns, result = asyncio.run(_drive())

    assert seen["released"] is True, "the event loop was parked for the whole phase"
    assert seen["worker_thread"] != seen["loop_thread"]
    assert turns >= 1
    assert "ok" in str(result.content)


@pytest.mark.parametrize(("tool_cls", "module", "key"), _PHASE_TOOLS)
def test_phase_tool_over_budget_returns_a_bounded_result(job, tool_cls, module, key, monkeypatch):
    """Expiry is a readable envelope plus a line on the timeline the user watches."""
    store, session_id = job
    monkeypatch.setattr(module, "get_config_value", _budget_config(key, 0.05))
    release = threading.Event()

    def _wedged_body(self, action_step):
        release.wait(timeout=_SAFETY_S)
        return MockSpeaker(content="{'ok': True}")

    tool = tool_cls(session_id=session_id)

    async def _drive():
        result = await tool.handle(MagicMock(tool_input={}))
        # The worker outlives the call — exactly what the message reports. Let it
        # go before the loop shuts its executor down.
        release.set()
        return result

    with (
        patch.object(module, "_resolve_runtime", return_value=MagicMock(wiki_store=store)),
        patch.object(tool_cls, "_handle_blocking", _wedged_body),
    ):
        result = asyncio.run(_drive())

    body = str(result.content)
    assert "'code': 'timeout'" in body
    assert "exceeded its budget" in body
    assert f"do not call {tool_cls.tool_id} again" in body

    warns = [
        e
        for e in store.load_job_events("j1")
        if e.get("type") == "log"
        and e.get("level") == "warn"
        and "budget" in str(e.get("text", ""))
    ]
    assert warns, "the job timeline must carry the same fact the model was told"


@pytest.mark.parametrize(("tool_cls", "module", "key"), _PHASE_TOOLS)
def test_phase_tool_declares_a_ceiling_above_its_budget(tool_cls, module, key):
    """Drive the real loop seam — an undeclared ceiling is the registry's 120s.

    A clone's own per-attempt git cap is 300s, so the flat fallback would kill
    every acquisition that had to try a second credential.
    """
    loop = object.__new__(ToolUseLoop)
    loop._tool_registry = MagicMock()
    loop._tool_registry.get_spec = MagicMock(return_value=None)
    tool = tool_cls(session_id="sess-1")
    loop._session_tools = [tool]

    ceiling = loop._tool_execution_timeout(
        tool_cls.tool_id, {"name": tool_cls.tool_id, "id": "call_1", "args": {}}
    )

    assert ceiling is not None
    assert ceiling > tool._budget_s(), "the tool must expire first"
    assert ceiling > 120.0, "the registry fallback would clip a real phase"


@pytest.mark.parametrize(("tool_cls", "module", "key"), _PHASE_TOOLS)
def test_phase_tool_budget_is_operator_tunable(tool_cls, module, key, monkeypatch):
    """The budget resolves from config, and the declared ceiling tracks it."""
    monkeypatch.setattr(module, "get_config_value", _budget_config(key, 123.0))
    tool = tool_cls(session_id="sess-1")

    assert tool._budget_s() == 123.0
    assert tool.execution_timeout({}) == 123.0 + module._CEILING_MARGIN_S
