"""``wiki_build_graph`` must not own the event loop for the length of a build.

The defect these cover: ``handle`` was an ``async def`` whose body — tree-sitter,
blocking driver writes, a synchronous embedding call — contained no suspension
point at all, and the loop awaited it inline. One live call held the loop for
roughly twenty-five minutes, during which nothing else on it could run.

The loop's own ``asyncio.wait_for`` ceiling could not save it, and that is worth
stating precisely because it looks like it should have: a timeout is a loop
timer, and a loop that is not turning runs no timers. Measured on this
interpreter, a 0.1s ceiling over a coroutine that ``time.sleep``s for 1s returns
the value after the full second and never raises. So both halves are tested
here — the work runs off the loop, AND the ceiling that only now can fire is
declared at a size a real build fits inside.

No test here sleeps to pass. The blocking stand-in blocks on an event that a
concurrent coroutine sets, so the fixed path proceeds the instant the loop
turns; the bounded wait exists only so a regression FAILS rather than hangs.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_graph.plugins.wiki import build_graph as build_graph_mod
from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

# Only a regression ever waits this long: on the fixed path the handshake below
# completes as soon as the event loop takes its next turn.
_SAFETY_S = 3.0
# Ceiling on the driver's polling turns, so a worker that never starts ends the
# test instead of spinning forever.
_MAX_TURNS = 400


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


def _runtime(store):
    return MagicMock(wiki_store=store)


def test_build_runs_off_the_loop_and_the_loop_keeps_turning(job):
    """A concurrent coroutine must make progress while the build is in flight.

    The handshake is the assertion: the worker blocks until ``release``, and only
    the event loop can set it. If the build owns the loop, the release never
    arrives and the worker falls out on its own bounded wait instead.
    """
    store, session_id = job
    entered = threading.Event()
    release = threading.Event()
    seen: dict[str, object] = {}

    def _blocking_core(ctx):
        seen["worker_thread"] = threading.get_ident()
        entered.set()
        seen["released"] = release.wait(timeout=_SAFETY_S)
        return {"nodeCount": 1, "edgeCount": 2}

    async def _drive():
        seen["loop_thread"] = threading.get_ident()
        tool = WikiBuildGraphTool(session_id=session_id)
        task = asyncio.create_task(tool.handle(MagicMock(tool_input={})))
        turns = 0
        while not entered.is_set() and turns < _MAX_TURNS:
            await asyncio.sleep(0.005)
            turns += 1
        release.set()
        return turns, await task

    with (
        patch.object(build_graph_mod, "_resolve_runtime", return_value=_runtime(store)),
        patch.object(build_graph_mod, "build_graph_core", _blocking_core),
    ):
        turns, result = asyncio.run(_drive())

    assert seen["released"] is True, "the event loop was parked for the whole build"
    assert seen["worker_thread"] != seen["loop_thread"]
    assert turns >= 1
    assert "nodeCount" in str(result.content)


def test_over_budget_build_returns_a_bounded_actionable_result(job, monkeypatch):
    """Expiry is a readable result, a timeline line and an honest trace title.

    Not a silent kill and not a bare "timed out": the worker cannot be stopped,
    so the outcome has to say that the parse continues and that re-running it
    would double up on the same tree.
    """
    store, session_id = job
    # Inject the BUDGET, not the module default: ``wiki.phase_timeouts.graph_build_s``
    # is a real typed field, so a resolvable config value beats the default and
    # patching the constant alone would leave the tool waiting the full hour.
    monkeypatch.setattr(
        build_graph_mod,
        "get_config_value",
        lambda *keys, default=None: 0.05
        if keys == ("wiki", "phase_timeouts", "graph_build_s")
        else default,
    )
    release = threading.Event()

    def _wedged_core(ctx):
        release.wait(timeout=_SAFETY_S)
        return {"nodeCount": 0, "edgeCount": 0}

    tool = WikiBuildGraphTool(session_id=session_id)

    async def _drive():
        result = await tool.handle(MagicMock(tool_input={}))
        # The worker outlives the call — exactly what the message reports. Let it
        # go before the loop shuts its executor down, so the test does not pay
        # the stand-in's bounded wait.
        release.set()
        return result

    with (
        patch.object(build_graph_mod, "_resolve_runtime", return_value=_runtime(store)),
        patch.object(build_graph_mod, "build_graph_core", _wedged_core),
    ):
        result = asyncio.run(_drive())

    body = str(result.content)
    assert "'code': 'timeout'" in body
    assert "exceeded its budget" in body
    assert "do not call wiki_build_graph again" in body

    events = store.load_job_events("j1")
    warns = [
        e
        for e in events
        if e.get("type") == "log"
        and e.get("level") == "warn"
        and "budget" in str(e.get("text", ""))
    ]
    assert warns, "the job timeline must carry the same fact the model was told"

    headline = tool.result_headline()
    assert headline is not None and "budget" in headline


def test_budget_is_operator_tunable_and_both_readers_follow_it(monkeypatch):
    """The budget comes from config, and the loop ceiling tracks it.

    Two readers resolve this number — the wait around the build and the ceiling
    declared to the loop. If only one honoured the operator's value, a deployment
    that raised the budget would have the loop clip the build at the old one, and
    the failure would look like the tool ignoring its own setting.
    """
    def _fake_config(*keys, default=None):
        if keys == ("wiki", "phase_timeouts", "graph_build_s"):
            return 900.0
        return default

    monkeypatch.setattr(build_graph_mod, "get_config_value", _fake_config)
    tool = WikiBuildGraphTool(session_id="sess-1")

    assert tool._budget_s() == 900.0
    assert tool.execution_timeout({}) == 900.0 + build_graph_mod._CEILING_MARGIN_S


def test_budget_falls_back_to_the_module_default_when_unset(monkeypatch):
    """An unset knob resolves to the shipped default — today's behaviour exactly."""
    monkeypatch.setattr(
        build_graph_mod, "get_config_value", lambda *keys, default=None: default
    )
    tool = WikiBuildGraphTool(session_id="sess-1")

    assert tool._budget_s() == build_graph_mod._GRAPH_BUILD_BUDGET_S


def test_loop_ceiling_is_the_tools_own_budget_not_the_registry_default():
    """Drive the real loop seam: an undeclared ceiling would kill every build.

    A SessionTool has no ``ToolSpec``, so ``_tool_execution_timeout`` falls back
    to a flat 120s. That was survivable only while the ceiling could never fire;
    off-loop it is live, and a build runs for many multiples of it. This asserts
    against the loop's own resolution rather than the tool's hook in isolation,
    because the two sides are what have to agree.
    """
    loop = object.__new__(ToolUseLoop)
    loop._tool_registry = MagicMock()
    loop._tool_registry.get_spec = MagicMock(return_value=None)
    loop._session_tools = [WikiBuildGraphTool(session_id="sess-1")]

    ceiling = loop._tool_execution_timeout(
        "wiki_build_graph",
        {"name": "wiki_build_graph", "id": "call_1", "args": {}},
    )

    assert ceiling is not None
    assert ceiling > build_graph_mod._GRAPH_BUILD_BUDGET_S, "the tool must expire first"
    assert ceiling > 120.0, "the registry fallback would clip a real build"
