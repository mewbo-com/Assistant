#!/usr/bin/env python3
"""A SessionTool declares the headline of its own ``tool_result`` event.

If ``summary`` is a PREFIX of ``result`` it can only restate the payload. A
resumed wiki index reusing its checkout then renders as a row visually
identical to the first turn's — ``'reused': True`` being the sixth key of a
dict literal — and a reader concludes the repo was cloned twice when the reuse
guard worked perfectly.

Contract-first: the real loop, the real tools, the real emit path. Only the LLM
call and git are stubbed.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mewbo_core.common import MockSpeaker
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.tooling.session_tools import SESSION_TOOL_HEADLINE_MAX_CHARS
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_context,
    _make_hook_manager,
    _make_registry,
    _text_response,
    _tool_call_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PlainSessionTool:
    """A SessionTool that declares NO headline — the backward-compat control.

    Deliberately a standalone implementer (not a subclass of anything): the
    ``SessionTool`` Protocol inherits no default method bodies, so this is
    exactly the shape a plugin author writes and the shape the ``getattr``
    convention has to tolerate.
    """

    tool_id = "probe_tool"
    max_result_chars = 2000

    def __init__(self, *, content: str) -> None:
        self._content = content
        self.modes = frozenset({"act"})
        self.schema = {
            "type": "function",
            "function": {
                "name": "probe_tool",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def handle(self, action_step):  # noqa: ANN001 — mirrors SessionTool
        return MockSpeaker(content=self._content)

    def should_terminate_run(self) -> bool:
        return False

    def terminal_reason(self) -> str:
        return "completed"


class _HeadlineSessionTool(_PlainSessionTool):
    """A SessionTool that DOES declare a headline, recorded per call."""

    def __init__(self, *, content: str, headline, raises: bool = False) -> None:
        super().__init__(content=content)
        self._headlines = list(headline) if isinstance(headline, list) else [headline]
        self._raises = raises
        self._pending: str | None = None

    async def handle(self, action_step):  # noqa: ANN001 — mirrors SessionTool
        self._pending = self._headlines.pop(0) if self._headlines else None
        return MockSpeaker(content=self._content)

    def result_headline(self) -> str | None:
        if self._raises:
            raise RuntimeError("headline hook exploded")
        headline, self._pending = self._pending, None
        return headline


def _run_one_call(tool, *, calls: int = 1) -> list[dict]:
    """Drive *calls* real tool calls to *tool* through a real loop; return events."""
    events: list[dict] = []
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        side_effect=[
            *(
                _tool_call_response(tool.tool_id, {}, f"call_{i}")
                for i in range(1, calls + 1)
            ),
            _text_response("done"),
        ]
    )
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=_make_agent_context(event_logger=events.append),
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="sess-headline",
            extra_session_tools=[tool],
        )
        asyncio.run(loop.run("probe", tool_specs=[], context=_make_context()))
    return [e["payload"] for e in events if e["type"] == "tool_result"]


# ---------------------------------------------------------------------------
# The backward-compatibility guard — the property that matters most
# ---------------------------------------------------------------------------


def test_tool_declaring_no_headline_emits_todays_event_unchanged() -> None:
    """No declaration ⇒ the event is exactly what it was before the seam existed.

    Asserted on the WHOLE payload, not just ``summary``: a new key would be as
    much of a break for a consumer as a changed one. That is why `tool_call_id`
    appears below rather than being excluded from the comparison — it was added
    deliberately (the correlation key pairing this outcome with the `tool_call`
    event emitted before dispatch), and this assertion is the gate that made the
    addition a decision instead of a side effect. Keep it exhaustive.
    """
    payload = _run_one_call(_PlainSessionTool(content="{'ok': True}"))[0]

    assert payload == {
        "tool_call_id": "call_1",
        "tool_id": "probe_tool",
        "operation": payload["operation"],
        "tool_input": {},
        "result": "{'ok': True}",
        "success": True,
        # The rule when nothing is declared: summary IS the payload (under the cap).
        "summary": "{'ok': True}",
        "agent_id": payload["agent_id"],
        "model": "test-model",
    }


def test_undeclared_summary_is_still_the_capped_prefix() -> None:
    """The truncating arm of the old rule is untouched for an undeclared tool."""
    tool = _PlainSessionTool(content="x" * 5000)
    tool.max_result_chars = 100

    payload = _run_one_call(tool)[0]

    assert payload["summary"] == "x" * 100
    assert payload["result"].startswith("x" * 100)
    assert len(payload["result"]) > 100


# ---------------------------------------------------------------------------
# A declared headline titles the event
# ---------------------------------------------------------------------------


def test_declared_headline_titles_the_event_and_keeps_the_payload() -> None:
    """``summary`` becomes the tool's line; ``result`` still carries everything."""
    payload = _run_one_call(
        _HeadlineSessionTool(
            content="{'totalCount': 1964, 'reused': True}",
            headline="Reused existing clone at a715e88 (1964 files)",
        )
    )[0]

    assert payload["summary"] == "Reused existing clone at a715e88 (1964 files)"
    # The model's view is unchanged — the headline is a LOG title, not a result.
    assert payload["result"] == "{'totalCount': 1964, 'reused': True}"
    assert payload["success"] is True


def test_headline_is_drained_and_cannot_title_a_later_call() -> None:
    """A call that records nothing falls back to the prefix, not the last title."""
    payloads = _run_one_call(
        _HeadlineSessionTool(
            content="{'ok': True}",
            headline=["Cloned at a715e88 (1964 files)", None],
        ),
        calls=2,
    )

    assert payloads[0]["summary"] == "Cloned at a715e88 (1964 files)"
    assert payloads[1]["summary"] == "{'ok': True}"


def test_headline_is_clamped_to_one_bounded_markup_free_line() -> None:
    """Bounded, single-line, and cut at markup — it is a title, not a body."""
    payload = _run_one_call(
        _HeadlineSessionTool(
            content="{'ok': True}",
            headline="Built graph:\n  9 nodes <html><title>proxy.internal</title>",
        )
    )[0]

    assert payload["summary"] == "Built graph: 9 nodes"

    long_payload = _run_one_call(
        _HeadlineSessionTool(content="{'ok': True}", headline="n" * 900)
    )[0]
    assert len(long_payload["summary"]) == SESSION_TOOL_HEADLINE_MAX_CHARS


def test_raising_headline_hook_neither_breaks_the_call_nor_the_event() -> None:
    """A title must never be able to fail a tool call — best-effort, isolated."""
    payloads = _run_one_call(
        _HeadlineSessionTool(content="{'ok': True}", headline="never read", raises=True)
    )

    assert len(payloads) == 1
    assert payloads[0]["success"] is True
    assert payloads[0]["summary"] == "{'ok': True}"
    assert payloads[0]["result"] == "{'ok': True}"


# ---------------------------------------------------------------------------
# wiki_clone_repo — reused vs freshly cloned, through the real loop
# ---------------------------------------------------------------------------

_SHA = "a715e88" + "0" * 33


class _GitStub:
    """Stub for ``subprocess.run``: materialises a checkout, answers rev-parse.

    The patch is process-wide, so the loop's OWN git-context probe lands here
    too — hence the text-mode branch (that caller passes ``text=True`` and would
    choke on bytes) and the empty default answer, which reads to it as "not a
    repo" and leaves the loop's behaviour alone.
    """

    def __init__(self, clone_dir: Path, *, head_sha: str) -> None:
        self.clone_dir = clone_dir
        self.head_sha = head_sha
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if "clone" in cmd or "fetch" in cmd or "init" in cmd:
            self.clone_dir.mkdir(parents=True, exist_ok=True)
            (self.clone_dir / ".git").mkdir(exist_ok=True)
            (self.clone_dir / "README.md").write_text("hello")
        # The tool addresses its checkout with ``git -C <clone_dir>``; the
        # loop's own probe never names it, which is what separates the two.
        if "rev-parse" in cmd and str(self.clone_dir) in cmd:
            out = b"main\n" if "--abbrev-ref" in cmd else self.head_sha.encode() + b"\n"
            return self._done(cmd, out, kwargs)
        return self._done(cmd, b"", kwargs)

    @staticmethod
    def _done(cmd, out: bytes, kwargs) -> subprocess.CompletedProcess:
        if kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding"):
            return subprocess.CompletedProcess(cmd, 0, stdout=out.decode(), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")

    def ran(self, token: str) -> bool:
        return any(token in call for call in self.calls)


def _clone_summary(
    base: Path, monkeypatch: pytest.MonkeyPatch, *, pinned: bool
) -> tuple[str, _GitStub]:
    """Run wiki_clone_repo through the real loop; return its event summary."""
    import mewbo_graph.plugins.wiki.clone as clone_mod
    from mewbo_graph.plugins.wiki.clone import WikiCloneRepoTool
    from mewbo_graph.wiki.store import JsonWikiStore
    from mewbo_graph.wiki.types import IndexingJob

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(base / "clones"))
    store = JsonWikiStore(root_dir=base / "wiki")
    store.create_job(
        IndexingJob(
            jobId="job-h1", slug="org/repo", status="queued",
            scannedCount=0, totalCount=0, currentFile=None,
            commitSha=_SHA if pinned else None, branch="main",
        )
    )
    store.attach_job_session("job-h1", "sess-headline")

    clone_dir = base / "clones" / "job-h1"
    if pinned:
        # The resume shape: the checkout is already there, at the pinned commit.
        (clone_dir / ".git").mkdir(parents=True)
        (clone_dir / "README.md").write_text("already here")
    git = _GitStub(clone_dir, head_sha=_SHA)

    tool = WikiCloneRepoTool(session_id="sess-headline")
    with patch.object(
        clone_mod, "_resolve_runtime", return_value=SimpleNamespace(wiki_store=store)
    ), patch("subprocess.run", side_effect=git):
        payloads = _run_one_call_with_input(
            tool, {"url": "https://git.example.com/org/repo"}
        )
    return payloads[0]["summary"], git


def _run_one_call_with_input(tool, tool_input: dict) -> list[dict]:
    """``_run_one_call`` with real arguments on the tool call."""
    events: list[dict] = []
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=[
            _tool_call_response(tool.tool_id, tool_input, "call_1"),
            _text_response("done"),
        ]
    )
    with patch("mewbo_core.loop.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound
        loop = ToolUseLoop(
            agent_context=_make_agent_context(event_logger=events.append),
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id="sess-headline",
            extra_session_tools=[tool],
        )
        asyncio.run(loop.run("clone it", tool_specs=[], context=_make_context()))
    return [e["payload"] for e in events if e["type"] == "tool_result"]


def test_clone_headline_distinguishes_a_reuse_from_a_fresh_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact misdiagnosis: a reuse must not read as a second clone."""
    reused, reuse_git = _clone_summary(tmp_path / "resumed", monkeypatch, pinned=True)
    assert reused == "Reused existing clone at a715e88 (1 files)"
    assert not reuse_git.ran("clone")  # the guard really did skip the network

    cloned, fresh_git = _clone_summary(tmp_path / "first", monkeypatch, pinned=False)
    assert cloned == "Cloned at a715e88 (1 files)"
    assert fresh_git.ran("clone")


# ---------------------------------------------------------------------------
# wiki_build_graph — counts, and honesty about a skip
# ---------------------------------------------------------------------------


def _build_graph_headline(ctx) -> str | None:
    """Run wiki_build_graph against a stubbed ctx and drain its headline."""
    from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool

    tool = WikiBuildGraphTool(session_id="sess-headline")
    step = MagicMock()
    step.tool_input = {}
    # ``_job_ctx`` is the store/runtime boundary — the only thing stubbed here.
    with patch.object(WikiBuildGraphTool, "_job_ctx", return_value=ctx):
        asyncio.run(tool.handle(step))
    return tool.result_headline()


def test_build_graph_headline_counts_nodes_and_edges(tmp_path: Path) -> None:
    """The built case names both counts."""
    ctx = SimpleNamespace(
        slug="org/repo",
        job_id="job-h2",
        clone_dir=tmp_path,
        store=MagicMock(),
        resume_plan=None,
    )
    with patch(
        "mewbo_graph.plugins.wiki.build_graph.build_graph_core",
        return_value={"nodeCount": 1234, "edgeCount": 5678, "embeddedCount": 0},
    ):
        assert _build_graph_headline(ctx) == "Built graph: 1234 nodes, 5678 edges"


def test_build_graph_headline_says_when_the_phase_was_skipped(tmp_path: Path) -> None:
    """A checkpoint skip and a rebuild are the same-shaped step without this."""
    resume_plan = MagicMock()
    resume_plan.should_skip.side_effect = lambda phase: phase == "graph"
    resume_plan.node_count = 1234
    ctx = SimpleNamespace(
        slug="org/repo",
        job_id="job-h3",
        clone_dir=tmp_path,
        store=MagicMock(),
        resume_plan=resume_plan,
    )

    assert (
        _build_graph_headline(ctx)
        == "Graph reused from checkpoint: 1234 nodes (skipped on resume)"
    )
