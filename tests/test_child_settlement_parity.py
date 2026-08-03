#!/usr/bin/env python3
"""Parity tests for the ONE way a spawned child reaches its terminal state.

A child settles down one of two paths — the blocking nested spawn inside
``SpawnAgentTool._spawn_one``, and the background manager
``SpawnAgentTool._run_child_lifecycle`` that a ROOT fan-out uses. They used to
carry their own copies of the settle sequence, and the copies had drifted: each
named the model on a failed child's ``RunError`` differently, one as the
spawn's resolved model and one as the child context's.

Those two spell the SAME value, so no assertion over inputs could have caught
it and none here pretends to: what these tests pin is that the two paths AGREE,
which is the property a second copy silently loses. A change that reaches one
copy and not the other — the ordinary fate of duplicated code — fails here.

So they never assert a hand-typed expected payload. They drive the real spawn
path twice and compare the two settlements against each other.

The only stub is the child loop itself (the model boundary). ``mark_done``, the
lifecycle event emission, the attestation seam and the ``AgentResult`` build all
run for real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import get_args

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor, AgentResultStatus, AgentStatus
from mewbo_core.agents.spawn_agent import SettledStatus, SpawnOutcomeStatus
from mewbo_core.classes import ActionStep
from mewbo_core.contracts.run_error import RunError
from mewbo_core.contracts.types import Event
from test_honest_terminal_state import _settled, _spawn_tool, _spec, _stub_child_loop

# Identity is expected to differ between the two paths — a root's child sits one
# level higher than a nested one — so parity is asserted over everything else.
_IDENTITY_KEYS = ("agent_id", "parent_id", "depth")


def _sub_agent_payloads(events: list[Event]) -> list[dict]:
    """Every ``sub_agent`` lifecycle payload, as a plain dict.

    ``EventPayload`` is a union of TypedDicts and this one rides its generic
    catch-all arm, so the payload is read as a mapping rather than indexed
    through a key no single arm declares.
    """
    return [dict(e["payload"]) for e in events if e.get("type") == "sub_agent"]


def _stop_payload(events: list[Event]) -> dict:
    """The ONE terminal ``stop`` payload, with per-path identity stripped."""
    stops = [p for p in _sub_agent_payloads(events) if p.get("action") == "stop"]
    assert len(stops) == 1, f"expected exactly one terminal stop, got {len(stops)}"
    return {k: v for k, v in stops[0].items() if k not in _IDENTITY_KEYS}


class _ModelRecorder:
    """Stands in for ``RunError`` and records the ``model=`` each settle names.

    Delegates to the real classmethod so ``title``/``brief()`` keep behaving —
    the thing under test is WHICH model name the settle path hands in, which is
    not otherwise observable: it only reaches ``RunError.provider``, and no
    field of the resulting ``AgentResult`` carries that.
    """

    def __init__(self) -> None:
        self.models: list[str | None] = []

    def from_exception(self, exc: BaseException, *, model: str | None = None) -> RunError:
        """Record *model*, then classify for real."""
        self.models.append(model)
        return RunError.from_exception(exc, model=model)


def _drive_nested(outcome, *, spawn_args: dict | None = None, model_name: str = "model-a"):
    """Run ONE child down the blocking (depth>=1) path.

    Returns ``(events, spawn_outcome)``. There is deliberately no handle: this
    path unregisters its child in ``_spawn_one``'s ``finally``, so the emitted
    ``stop`` event IS the record of how it settled — which is the point of
    routing every terminal through one emitter.
    """

    async def _run():
        events: list[Event] = []
        hv = AgentHypervisor()
        root = AgentContext.root(
            model_name=model_name, registry=hv, event_logger=events.append
        )
        tool = _spawn_tool(root.child(model_name=model_name))
        tool.parent_tool_specs = [_spec()]
        _stub_child_loop(tool, outcome)
        spawn = await tool._spawn_one(dict(spawn_args or {"task": "probe"}))
        return events, spawn

    return asyncio.run(_run())


def _drive_root(outcome, *, spawn_args: dict | None = None, model_name: str = "model-a"):
    """Run ONE child down the ROOT lifecycle-manager path. Returns (events, handle)."""

    async def _run():
        events: list[Event] = []
        hv = AgentHypervisor()
        ctx = AgentContext.root(
            model_name=model_name, registry=hv, event_logger=events.append
        )
        tool = _spawn_tool(ctx)
        tool.parent_tool_specs = [_spec()]
        _stub_child_loop(tool, outcome)
        spawn = await tool._spawn_one(dict(spawn_args or {"task": "probe"}))
        assert spawn.status == "submitted", "a root spawn is non-blocking"
        await tool.await_lifecycle_managers(timeout=2.0)
        handles = await hv.list_all()
        return events, spawn, handles[0]

    return asyncio.run(_run())


def _check_agents_after(outcome, *, spawn_args: dict | None = None) -> dict:
    """Settle ONE root child, then read the payload its parent actually gets."""

    async def _run():
        hv = AgentHypervisor()
        ctx = AgentContext.root(model_name="model-a", registry=hv, event_logger=lambda _e: None)
        tool = _spawn_tool(ctx)
        tool.parent_tool_specs = [_spec()]
        _stub_child_loop(tool, outcome)
        await tool._spawn_one(dict(spawn_args or {"task": "probe"}))
        await tool.await_lifecycle_managers(timeout=2.0)
        speaker = await tool.handle_check_agents(
            ActionStep(tool_id="check_agents", operation="get", tool_input={})
        )
        return json.loads(speaker.content)

    return asyncio.run(_run())


# ===========================================================================
# The drift itself: which model a failed child's error names
# ===========================================================================


class TestFailedChildNamesOneModel:
    """Both settle paths must classify the failure against the SAME model."""

    def _recorded_model(self, drive, monkeypatch, **kwargs) -> str | None:
        recorder = _ModelRecorder()
        monkeypatch.setattr("mewbo_core.agents.spawn_agent.RunError", recorder)
        drive(RuntimeError("child loop exploded"), **kwargs)
        assert recorder.models, "the settle path never classified the failure"
        return recorder.models[-1]

    def test_both_paths_name_the_same_model(self, monkeypatch):
        nested = self._recorded_model(_drive_nested, monkeypatch)
        root = self._recorded_model(_drive_root, monkeypatch)
        assert nested == root == "model-a"

    def test_the_model_is_the_one_the_child_loop_ran_on(self, monkeypatch):
        """A per-spawn ``model`` override moves the name on BOTH paths.

        The parent stays on ``model-a``, so this pins that the settle names the
        model the CHILD ran on rather than its spawner's — the property the one
        surviving spelling (``child_ctx.model_name``) is chosen for.
        """
        args = {"task": "probe", "model": "model-b"}
        nested = self._recorded_model(_drive_nested, monkeypatch, spawn_args=args)
        root = self._recorded_model(_drive_root, monkeypatch, spawn_args=args)
        assert nested == root == "model-b"


# ===========================================================================
# Whole-terminal parity — the event, the registry, the Communication Unit
# ===========================================================================


class TestTerminalParity:
    """A child that ends the same way is reported the same way on both paths."""

    @pytest.mark.parametrize("done_reason", ["completed", "halted_no_progress"])
    def test_settled_stop_event_matches(self, done_reason):
        outcome = _settled(done_reason, task_result="what I found")
        nested_events, _nested_spawn = _drive_nested(outcome)
        root_events, _root_spawn, _root_handle = _drive_root(outcome)
        assert _stop_payload(nested_events) == _stop_payload(root_events)

    @pytest.mark.parametrize("done_reason", ["completed", "halted_no_progress"])
    def test_settled_communication_unit_matches(self, done_reason):
        """The parent's inline result and the handle's stored one are one value.

        The nested path RETURNS the ``AgentResult`` as its tool payload; the
        root path stores it on the handle for ``check_agents`` to collect.
        Same settle, so the two must deserialize to the same record — content,
        status, summary, attempts, CU shape and verifier provenance alike.
        """
        outcome = _settled(done_reason, task_result="what I found")
        _nested_events, nested_spawn = _drive_nested(outcome)
        _root_events, _root_spawn, root_handle = _drive_root(outcome)
        assert root_handle.result is not None
        assert json.loads(nested_spawn.content) == asdict(root_handle.result)
        assert root_handle.result.content == "what I found"

    def test_failed_stop_event_matches(self):
        outcome = RuntimeError("child loop exploded")
        nested_events, nested_spawn = _drive_nested(outcome)
        root_events, _root_spawn, root_handle = _drive_root(outcome)
        assert _stop_payload(nested_events) == _stop_payload(root_events)
        assert nested_spawn.status == "failed"
        assert root_handle.status == "failed"

    def test_failed_communication_unit_matches(self):
        """Both paths hand back the same bounded blurb, never a raw exception."""
        outcome = RuntimeError("child loop exploded")
        _nested_events, nested_spawn = _drive_nested(outcome)
        _root_events, _root_spawn, root_handle = _drive_root(outcome)
        assert root_handle.result is not None
        assert json.loads(nested_spawn.content) == asdict(root_handle.result)
        assert root_handle.result.content.startswith("Sub-agent failed: ")


# ===========================================================================
# The one asymmetry the settle KEEPS
# ===========================================================================


class TestStatusVocabularies:
    """Four vocabularies, one containment law — checked, not asserted in prose.

    ``AgentStatus`` is the authority: where a REGISTERED agent sits in its
    lifecycle. ``AgentResultStatus`` is what a child ACHIEVED. Neither contains
    the other, and a settle has to satisfy BOTH — it calls ``mark_done`` with
    one value and builds an ``AgentResult`` with the same one. ``SettledStatus``
    is therefore exactly their intersection, and ``SpawnOutcomeStatus`` is that
    plus the outcomes reachable only at the spawn call.

    A type-checker catches a bad assignment, but only in a build that runs the
    stricter one. This runs in the suite.
    """

    def test_settled_status_is_exactly_the_intersection(self):
        """The law. Widening any vocabulary without the others fails HERE.

        It also states, as a checked fact, which members are OUTSIDE a settle:
        ``submitted``/``running`` (non-terminal), ``rejected`` (decided before
        registration), and the two achievement verdicts ``cannot_solve`` and
        ``partial``, which no settle can mint.
        """
        assert set(get_args(SettledStatus)) == (
            set(get_args(AgentStatus)) & set(get_args(AgentResultStatus))
        )

    def test_a_settle_can_satisfy_both_consumers(self):
        """Why the intersection is the right type and not merely a tidy one."""
        settled = set(get_args(SettledStatus))
        assert settled <= set(get_args(AgentStatus)), "mark_done + attestation"
        assert settled <= set(get_args(AgentResultStatus)), "AgentResult"
        assert settled <= set(get_args(SpawnOutcomeStatus)), "the spawn payload"

    def test_partial_is_dead_and_this_seam_does_not_spread_it(self):
        """``partial`` is declared one layer down and minted by nothing.

        Pinned rather than merely noted, because the tempting fix for the
        type error at the spawn seam was to admit it HERE — which would have
        propagated a dead member into a second vocabulary and onto the batch
        wire payload. Resolving it (mint it for budget-exhausted children, or
        delete it) is a change to ``AgentResultStatus`` and its client mirrors.
        """
        assert "partial" in get_args(AgentResultStatus)
        assert "partial" not in get_args(AgentStatus)
        assert "partial" not in get_args(SettledStatus)
        assert "partial" not in get_args(SpawnOutcomeStatus)


class TestModelOverrideIsHonoured:
    """A ``model`` override must not be the thing that fails a spawn.

    A field report described every spawn carrying a non-default ``model`` dying
    as ``failed`` at zero steps. These pin the harness half of that: supplying
    the argument routes the child onto that model and settles it normally. What
    they deliberately do NOT cover is whether the deployment's gateway can serve
    the name — ``_resolve_model`` waves any string through when
    ``agent.allowed_models`` is empty (its default), so an unservable id reaches
    the provider and the child dies on its first call. That is a config/routing
    axis, not a settle-path one, and no test here can stand in for it.
    """

    def test_a_healthy_child_settles_completed_with_an_override(self):
        _events, _spawn, handle = _drive_root(
            _settled("completed", task_result="what I found"),
            spawn_args={"task": "probe", "model": "model-b"},
        )
        assert handle.status == "completed"
        assert handle.result is not None
        assert handle.result.status == "completed"

    def test_the_child_runs_on_the_overridden_model(self):
        """The ``start`` and ``stop`` events both name the child's model."""
        events, _spawn, _handle = _drive_root(
            _settled("completed", task_result="what I found"),
            spawn_args={"task": "probe", "model": "model-b"},
        )
        models = {p["model"] for p in _sub_agent_payloads(events)}
        assert models == {"model-b"}, "the parent's model must not leak onto the child"

    def test_a_failed_child_hands_its_reason_to_check_agents(self):
        """``failed`` alone is not a diagnosis — the bounded reason rides with it.

        The same field report noted no error text beyond ``-> FAILED``. The
        settle stores a bounded ``AgentResult.content``, and this pins that it
        survives all the way into the payload the model actually reads.
        """
        payload = _check_agents_after(RuntimeError("gateway declined the model"))
        entry = next(a for a in payload["agents"] if a["result"] is not None)
        assert entry["status"] == "failed"
        assert entry["result"]["status"] == "failed"
        assert "gateway declined the model" in entry["result"]["content"]
        assert "gateway declined the model" in payload["text"]


class TestCancelledTerminal:
    """A cancelled child produced no Communication Unit, and says so."""

    def _drive(self):
        return _drive_root(
            asyncio.CancelledError(), spawn_args={"task": "probe", "summary_kind": "evidence"}
        )

    def test_settles_cancelled_with_its_own_detail(self):
        events, _spawn, handle = self._drive()
        payload = _stop_payload(events)
        assert payload["detail"] == "cancelled by parent"
        assert payload["status"] == "cancelled"
        assert handle.result is not None
        assert handle.result.status == "cancelled"

    def test_stop_event_declares_no_cu_shape(self):
        """``summary_kind`` rides WITH a summary — and there is none here.

        The declared kind is not lost: it stays on the ``AgentResult``, which is
        what a spawner reads. It is the EVENT that must not advertise the shape
        of a Communication Unit the child never produced.
        """
        events, _spawn, handle = self._drive()
        payload = _stop_payload(events)
        assert "summary" not in payload
        assert "summary_kind" not in payload
        assert handle.result is not None
        assert handle.result.summary_kind == "evidence"

    def test_a_completed_child_does_declare_its_cu_shape(self):
        """The contrast that makes the omission above a rule, not an accident."""
        events, _spawn, _handle = _drive_root(
            _settled("completed", task_result="what I found"),
            spawn_args={"task": "probe", "summary_kind": "evidence"},
        )
        payload = _stop_payload(events)
        assert payload["summary"] == "what I found"
        assert payload["summary_kind"] == "evidence"
