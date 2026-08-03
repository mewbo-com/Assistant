#!/usr/bin/env python3
"""Contract tests for per-spawn project scoping.

One session, one ``AgentHypervisor``, a fleet working in project A beside a
fleet working in project B. The seam is ``SpawnAgentTool``: a spawn resolves
its own :class:`ChildWorkspace` through the injected ``ProjectCatalog`` and
hands it to the child loop, instead of copying the parent's directory.

Every test drives the REAL spawn path — real catalog, real admission, real
child ``ToolUseLoop`` construction. The only stub is the child loop's ``run``,
which is the model boundary; that is what lets a test read back the directory
and the instructions a child was actually built with.
"""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHandle, AgentHypervisor
from mewbo_core.agents.spawn_agent import SpawnAgentTask, SpawnAgentTool
from mewbo_core.classes import ActionStep, OrchestrationState, TaskQueue
from mewbo_core.config import ProjectConfig
from mewbo_core.hooks import HookManager
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.tooling.tool_registry import ToolRegistry, ToolSpec
from mewbo_core.workspaces.project_catalog import ProjectCatalog
from pydantic import ValidationError

PARENT_INSTRUCTIONS = "PARENT-RULES: the directory the root agent started in."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _projects(tmp_path, *names: str) -> dict[str, str]:
    """Create one real directory per name and return name -> absolute path."""
    made: dict[str, str] = {}
    for name in names:
        directory = tmp_path / name
        directory.mkdir()
        made[name] = str(directory)
    return made


def _catalog(paths: dict[str, str]) -> ProjectCatalog:
    """A catalog over configured projects only — no store, no locator needed."""
    return ProjectCatalog(
        configured={name: ProjectConfig(path=path) for name, path in paths.items()}
    )


def _spec(tool_id: str = "shell_tool") -> ToolSpec:
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description=f"Test tool {tool_id}",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"schema": {"type": "object", "properties": {}}},
    )


def _spawn_tool(
    ctx: AgentContext,
    *,
    cwd: str | None = None,
    catalog: ProjectCatalog | None = None,
) -> SpawnAgentTool:
    registry = ToolRegistry()
    registry.register(_spec())
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    hooks = MagicMock(spec=HookManager)
    hooks.run_on_agent_start.return_value = None
    hooks.run_on_agent_stop.return_value = None
    return SpawnAgentTool(
        agent_context=ctx,
        tool_registry=registry,
        permission_policy=policy,
        hook_manager=hooks,
        project_instructions=PARENT_INSTRUCTIONS,
        cwd=cwd,
        catalog=catalog,
    )


def _delegating_ctx(hv: AgentHypervisor) -> AgentContext:
    """A depth-1 context, so ``_spawn_one`` takes the BLOCKING path.

    Blocking admission settles the child before ``run_async`` returns, which is
    what makes every workspace assertion here deterministic. The concurrent
    root path is exercised separately.
    """
    root = AgentContext.root(
        model_name="test-model", registry=hv, message_queue=queue.Queue()
    )
    return root.child(model_name="test-model")


async def _root_ctx(hv: AgentHypervisor) -> AgentContext:
    """A registered depth-0 context, so ``send_to_parent`` finds a queue."""
    root_q: queue.Queue[str] = queue.Queue()
    ctx = AgentContext.root(model_name="test-model", registry=hv, message_queue=root_q)
    await hv.register(
        AgentHandle(
            agent_id=ctx.agent_id,
            parent_id=None,
            depth=0,
            model_name=ctx.model_name,
            task_description="root",
            status="running",
            message_queue=root_q,
        )
    )
    return ctx


def _settled(done_reason: str = "completed") -> tuple[TaskQueue, OrchestrationState]:
    """A ``(tq, state)`` pair as a child loop that reached a terminal returns it."""
    tq = TaskQueue(_human_message="task", action_steps=[])
    tq.task_result = "child result"
    return tq, OrchestrationState(goal="task", done=True, done_reason=done_reason)


def _capture_children(
    tool: SpawnAgentTool,
    *,
    on_build: Any = None,
    outcomes: list[Any] | None = None,
) -> list[Any]:
    """Build the REAL child loop, then stub only its model-facing ``run``.

    Returns the list of loops as they are built, so a test can read back the
    ``cwd``, instructions and context each child was actually constructed with.
    ``on_build`` runs after each build — the seam a rebind-mid-flight test uses
    to move the tool between one attempt and the next.
    """
    built: list[Any] = []
    original = tool._build_child_loop

    def _build(*args: Any, **kwargs: Any) -> Any:
        loop = original(*args, **kwargs)
        index = len(built)
        built.append(loop)
        if outcomes:
            outcome = outcomes[min(index, len(outcomes) - 1)]
        else:
            outcome = _settled()
        loop.run = AsyncMock(return_value=outcome)
        if on_build is not None:
            on_build(index)
        return loop

    tool._build_child_loop = _build  # type: ignore[method-assign]
    return built


def _step(task: str = "do work", **extra: Any) -> ActionStep:
    return ActionStep(
        tool_id="spawn_agent", operation="set", tool_input={"task": task, **extra}
    )


def _batch_step(*tasks: dict[str, Any]) -> ActionStep:
    return ActionStep(
        tool_id="spawn_agents", operation="set", tool_input={"tasks": list(tasks)}
    )


# ---------------------------------------------------------------------------
# Per-spawn project resolution
# ---------------------------------------------------------------------------


class TestPerSpawnProject:
    """``project`` points ONE child at ONE workspace; absent inherits."""

    def test_two_children_in_two_projects_get_their_own_directories(self, tmp_path):
        """The headline case: one hypervisor, two fleets, two repositories."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            ctx = await _root_ctx(hv)
            tool = _spawn_tool(ctx, cwd=paths["alpha"], catalog=_catalog(paths))
            built = _capture_children(tool)

            # Root spawns are non-blocking, so both children are live at once.
            await tool.run_async(_step("work A", project="alpha"))
            await tool.run_async(_step("work B", project="beta"))
            await tool.await_lifecycle_managers(timeout=5.0)

            assert [loop._cwd for loop in built] == [paths["alpha"], paths["beta"]]
            # One hypervisor admitted both, and every slot came back.
            assert hv.free_slots == 5

        asyncio.run(_test())

    def test_an_omitted_project_inherits_the_parents_workspace(self, tmp_path):
        """No ``project`` ⇒ the pre-existing path, directory AND instructions."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            await tool.run_async(_step("work"))

            assert len(built) == 1
            assert built[0]._cwd == paths["alpha"]
            # The parent's own string, not a re-discovery of it.
            assert built[0]._project_instructions is PARENT_INSTRUCTIONS

        asyncio.run(_test())

    def test_an_omitted_project_needs_no_catalog_at_all(self, tmp_path):
        """A catalog-less deployment (the CLI) spawns exactly as it always did."""

        async def _test():
            paths = _projects(tmp_path, "alpha")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(_delegating_ctx(hv), cwd=paths["alpha"], catalog=None)
            built = _capture_children(tool)

            result = await tool.run_async(_step("work"))

            assert json.loads(result.content)["status"] == "completed"
            assert built[0]._cwd == paths["alpha"]

        asyncio.run(_test())

    def test_a_batch_gives_each_entry_its_own_project(self, tmp_path):
        """One ``spawn_agents`` call, three entries, three workspaces."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            result = await tool.run_batch_async(
                _batch_step(
                    {"task": "a", "project": "beta"},
                    {"task": "b", "project": "alpha"},
                    {"task": "c"},
                )
            )
            payload = json.loads(result.content)

            assert payload["spawned"] == 3
            assert payload["rejected"] == 0
            assert [loop._cwd for loop in built] == [
                paths["beta"],
                paths["alpha"],
                paths["alpha"],  # the entry that named nothing inherits
            ]

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Refusal — never a silent fallback
# ---------------------------------------------------------------------------


class TestUnresolvableProjectRefuses:
    """A key that resolves to nothing kills the spawn; it never degrades."""

    def test_an_unknown_key_rejects_the_slot(self, tmp_path):
        """Rejected, named, and no child was ever built in the parent's dir."""

        async def _test():
            paths = _projects(tmp_path, "alpha")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            result = await tool.run_async(_step("work", project="gamma"))

            refusal = json.loads(result.content)["error"]
            # PERMANENT, and typed as such: re-issuing this call unchanged
            # fails identically, so it must never be made to wait for a slot.
            assert refusal["code"] == "unresolvable_project"
            assert refusal["permanence"] == "permanent"
            assert "gamma" in refusal["message"]
            # The refusal NAMES what would have worked, so it is correctable.
            assert "alpha" in refusal["message"]
            assert built == []
            # No slot was taken, nothing scheduled, nothing registered.
            assert hv.free_slots == 5
            assert hv.pending_dispatch == 0
            assert await hv.list_all() == []

        asyncio.run(_test())

    def test_a_project_whose_directory_is_missing_rejects(self, tmp_path):
        """Listed but not on disk is still unrunnable — refuse, do not inherit."""

        async def _test():
            paths = _projects(tmp_path, "alpha")
            catalog = _catalog({**paths, "ghost": str(tmp_path / "ghost")})
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(_delegating_ctx(hv), cwd=paths["alpha"], catalog=catalog)
            built = _capture_children(tool)

            result = await tool.run_async(_step("work", project="ghost"))

            refusal = json.loads(result.content)["error"]
            assert refusal["code"] == "unresolvable_project"
            assert "ghost" in refusal["message"]
            assert built == []

        asyncio.run(_test())

    def test_a_project_without_a_catalog_is_refused_not_inherited(self, tmp_path):
        """No catalog ⇒ a named project is unanswerable, so it is refused."""

        async def _test():
            paths = _projects(tmp_path, "alpha")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(_delegating_ctx(hv), cwd=paths["alpha"], catalog=None)
            built = _capture_children(tool)

            result = await tool.run_async(_step("work", project="alpha"))

            refusal = json.loads(result.content)["error"]
            assert refusal["code"] == "unresolvable_project"
            assert "alpha" in refusal["message"]
            assert built == []

        asyncio.run(_test())

    def test_a_rejected_batch_entry_carries_its_reason(self, tmp_path):
        """A batch slot is the one place a refusal's reason would be discarded."""

        async def _test():
            paths = _projects(tmp_path, "alpha")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            result = await tool.run_batch_async(
                _batch_step({"task": "a", "project": "gamma"}, {"task": "b"})
            )
            payload = json.loads(result.content)

            assert payload["spawned"] == 1
            assert payload["rejected"] == 1
            refused = payload["agents"][0]
            assert refused["status"] == "rejected"
            assert refused["agent_id"] is None
            assert "gamma" in refused["reason"]
            # A refusal a caller can ACT on: this one will never resolve, so
            # it must not read the same as an entry that merely has to wait.
            assert refused["code"] == "unresolvable_project"
            # The sibling that named nothing is untouched.
            assert payload["agents"][1]["status"] == "completed"
            assert [loop._cwd for loop in built] == [paths["alpha"]]

        asyncio.run(_test())

    def test_a_misspelled_field_is_a_clean_refusal(self):
        """``extra="forbid"`` turns a typo into a validation error, not a no-op."""
        with pytest.raises(ValidationError):
            SpawnAgentTask.model_validate({"task": "work", "porject": "alpha"})
        # A blank key is refused at definition too — it names nothing.
        with pytest.raises(ValidationError):
            SpawnAgentTask.model_validate({"task": "work", "project": ""})


# ---------------------------------------------------------------------------
# Containment — a different directory is not more privilege
# ---------------------------------------------------------------------------


class TestContainmentStillNarrows:
    """Moving a child sideways must never move it upward."""

    def test_a_cross_project_child_cannot_widen_its_ceilings(self, tmp_path):
        """Both axes stay min-wins even when the child lands elsewhere."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            root = AgentContext.root(
                model_name="test-model", registry=hv, message_queue=queue.Queue()
            )
            # A parent already narrowed on BOTH axes.
            parent = root.child(
                model_name="test-model",
                capability_mode="read_only",
                workspace_mode="read_only",
            )
            tool = _spawn_tool(parent, cwd=paths["alpha"], catalog=_catalog(paths))
            built = _capture_children(tool)

            await tool.run_async(
                _step(
                    "work",
                    project="beta",
                    capability_mode="all",
                    workspace_mode="full_access",
                )
            )

            child_ctx = built[0]._ctx
            assert built[0]._cwd == paths["beta"]  # it did move directories
            assert child_ctx.capability_mode == "read_only"  # …but not upward
            assert child_ctx.workspace_mode == "read_only"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# rebind_cwd — future children only
# ---------------------------------------------------------------------------


class TestRebindCwd:
    """The session-level switch moves the NEXT child, never a live one."""

    def test_rebind_moves_the_children_spawned_after_it(self, tmp_path):
        """Directory and instructions both re-point for subsequent spawns."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            await tool.run_async(_step("before"))
            tool.rebind_cwd(paths["beta"], project_instructions="BETA-RULES")
            await tool.run_async(_step("after"))

            assert [loop._cwd for loop in built] == [paths["alpha"], paths["beta"]]
            assert built[0]._project_instructions == PARENT_INSTRUCTIONS
            assert built[1]._project_instructions == "BETA-RULES"

        asyncio.run(_test())

    def test_rebind_does_not_move_a_child_already_in_flight(self, tmp_path):
        """A spawn captures its workspace at admission and finishes where it started.

        Driven through the retry path, which is the one place a live child gets
        a SECOND loop built for it: a rebind between attempt one and attempt two
        must not relocate the agent mid-task.
        """

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )

            def _rebind_during_first_attempt(index: int) -> None:
                if index == 0:
                    tool.rebind_cwd(paths["beta"], project_instructions="BETA-RULES")

            built = _capture_children(
                tool,
                on_build=_rebind_during_first_attempt,
                # Attempt 1 stops short (the dominant child-death shape: a
                # RETURN, not a raise), so the retry driver builds attempt 2.
                outcomes=[_settled("halted_no_progress"), _settled("completed")],
            )

            await tool.run_async(_step("work", retry={"max": 1}))

            assert len(built) == 2  # the retry really did re-drive the child
            assert [loop._cwd for loop in built] == [paths["alpha"], paths["alpha"]]
            assert built[1]._project_instructions is PARENT_INSTRUCTIONS
            # The rebind still applies to the NEXT spawn.
            await tool.run_async(_step("next"))
            assert built[2]._cwd == paths["beta"]

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Instructions follow the directory
# ---------------------------------------------------------------------------


class TestProjectInstructions:
    """A child in another repository reads THAT repository's rules."""

    def test_a_cross_project_child_reads_the_new_projects_instructions(self, tmp_path):
        """Re-discovered from the child's cwd, never inherited from the parent."""

        async def _test():
            paths = _projects(tmp_path, "alpha", "beta")
            (tmp_path / "beta" / "CLAUDE.md").write_text(
                "BETA-PROJECT-RULES: only the beta checkout declares this.",
                encoding="utf-8",
            )
            hv = AgentHypervisor(max_concurrent=5)
            tool = _spawn_tool(
                _delegating_ctx(hv), cwd=paths["alpha"], catalog=_catalog(paths)
            )
            built = _capture_children(tool)

            await tool.run_async(_step("work", project="beta"))

            instructions = built[0]._project_instructions or ""
            assert "BETA-PROJECT-RULES" in instructions
            assert PARENT_INSTRUCTIONS not in instructions

        asyncio.run(_test())
