"""A timed-out code pipeline must stop WRITING, and must say so honestly.

The engine runs a ``mode="code"`` pipeline on a daemon thread and joins it with a
deadline. Python cannot kill that thread, so the join abandons the WAITER: before
this suite existed, the caller was told ``timeout`` while the abandoned thread kept
upserting through the real :class:`AppDataStore`, the ``failed`` ledger row recorded
``docs_written`` EMPTY (that field is read off the ``PipelineResult`` only success
produces), and the model saw a flat ``code:"execution"`` with nothing to act on.

Three contracts, one per lie:

* **The write stops.** Documents written BEFORE the deadline land; a write
  attempted after it is refused. The guard is cooperative
  (``PipelineContext.ensure_side_effects_allowed``) — the thread keeps burning CPU, it just
  stops changing the world.
* **The ledger is true.** The ``failed`` row carries the PARTIAL count, not zero.
* **The error is actionable.** ``run_pipeline`` surfaces the typed ``timeout``
  code and names ``app_data(operation='query', …)`` as the way to settle what
  actually landed.

Timings are sub-second and the caller SYNCHRONISES on the worker thread exiting
(``_join_pipeline_threads``) rather than sleeping a guessed interval — the
assertion is about what the abandoned thread did, so the test must observe it
after it is done trying.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
from mewbo_api.apps.models import (
    AppFrontend,
    AppSpec,
    CollectionSpec,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import AppPipelineRunner, PipelineExecutionError
from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker
from mewbo_api.apps.plugin.run_pipeline import (
    _MAX_PIPELINE_TIMEOUT_SECONDS,
    RunPipelineTool,
)
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_core.classes import ActionStep

NOW = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "maint-1"

#: The watchdog wall the runner is driven with — an override, since the model's
#: own ``timeout_seconds`` floor is 1 s and a suite must not spend it.
WATCHDOG_SECONDS = 0.1
#: How long the pipeline body sleeps past that wall before its second write.
OVERRUN_SECONDS = 0.4

# Writes one doc, overruns the deadline, then tries to write a second. The second
# write is the whole subject of this file: it is the one that used to land with no
# caller waiting and no ledger row describing it.
_OVERRUN_PIPELINE = (
    "import time\n"
    "def run(params, ctx):\n"
    "    ctx.collection('notes').upsert('before', {'phase': 'before'})\n"
    "    time.sleep(params['sleep'])\n"
    "    ctx.collection('notes').upsert('after', {'phase': 'after'})\n"
    "    return 'finished'\n"
)

_PARAMS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"sleep": {"type": "number"}},
    "required": ["sleep"],
}


def _pipeline(*, timeout_seconds: int = 10) -> PipelineSpec:
    return PipelineSpec(
        name="ingest",
        wake_prompt="ingest",
        mode="code",
        entrypoint="pipelines/ingest.py",
        on_demand=True,
        params_schema=_PARAMS_SCHEMA,
        timeout_seconds=timeout_seconds,
    )


def _app(pipeline: PipelineSpec) -> AppSpec:
    return AppSpec(
        app_id="app-x",
        title="App",
        owner_session_id="owner",
        maintainer_session_id=SESSION_ID,
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(
            files={"app.py": "import streamlit as st", "pipelines/ingest.py": _OVERRUN_PIPELINE}
        ),
        collections=[CollectionSpec(name="notes", json_schema={"type": "object"})],
        pipelines=[pipeline],
        status="live",
    )


def _stores(tmp_path):
    return (
        JsonAppStore(root_dir=tmp_path / "apps"),
        JsonAppDataStore(root_dir=tmp_path / "apps"),
        JsonPipelineRunStore(root_dir=tmp_path / "apps"),
    )


def _runner(app_store, data_store, *, watchdog: float | None = WATCHDOG_SECONDS):
    return AppPipelineRunner(
        app_store=app_store,
        app_data=data_store,
        workspace_resolver=lambda _app: None,
        clock=lambda: NOW,
        timeout_seconds=watchdog,
    )


def _live_pipeline_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "mewbo-pipeline" and t.is_alive()}


def _join_pipeline_threads(
    started_after: set[threading.Thread], limit_seconds: float = 5.0
) -> None:
    """Block until the workers THIS test abandoned have exited.

    The runner cannot join them (that is the defect being fixed), so the TEST
    does — otherwise an assertion about "the post-deadline write was refused"
    could pass merely by racing ahead of the thread that was about to make it.

    Scoped by the pre-call snapshot rather than by thread name, because a
    lingering worker is exactly what this module is about and one of them is
    ETERNAL: ``test_apps_pipeline_runner.py`` drives the watchdog with a
    deliberate infinite loop, whose thread outlives that test and every later
    one in the same process. Waiting on the name alone passes alone and fails in
    the suite, which is a bug in the test, not a leak in the run.
    """
    deadline = time.monotonic() + limit_seconds
    while time.monotonic() < deadline:
        if not (_live_pipeline_threads() - started_after):
            return
        time.sleep(0.02)
    pytest.fail("a mewbo-pipeline worker thread never exited")


class TestPostDeadlineWritesAreRefused:
    def test_a_write_attempted_after_the_deadline_never_lands(self, tmp_path):
        app_store, data_store, _ = _stores(tmp_path)
        pipeline = _pipeline()
        app = _app(pipeline)
        app_store.save(app)
        runner = _runner(app_store, data_store)

        prior = _live_pipeline_threads()
        with pytest.raises(PipelineExecutionError) as caught:
            runner.execute(app, pipeline, {"sleep": OVERRUN_SECONDS}, now=NOW)
        assert caught.value.code == "timeout"

        _join_pipeline_threads(prior)

        # The pre-deadline write is real and stays; the post-deadline one is gone.
        assert data_store.get("app-x", "notes", "before") is not None
        assert data_store.get("app-x", "notes", "after") is None

    def test_a_stopped_context_refuses_every_side_effecting_surface(self, tmp_path):
        # The guard is on the CONTEXT, not on one call site: exec and llm are
        # equally capable of reaching the world after the caller has gone.
        from mewbo_api.apps.pipeline_runner import PipelineContext

        _, data_store, _ = _stores(tmp_path)
        pipeline = _pipeline()
        ctx = PipelineContext(
            app=_app(pipeline),
            pipeline=pipeline,
            params={},
            now=NOW,
            workspace_root=str(tmp_path),
            data_store=data_store,
            dry_run=False,
            llm_step=lambda *_args: {"ok": True},
        )
        ctx.stop("stopped for the test")

        for call in (
            lambda: ctx.collection("notes").upsert("k", {"v": 1}),
            lambda: ctx.collection("notes").delete("k"),
            lambda: ctx.exec(["git", "status"]),
            lambda: ctx.llm("prompt", {"type": "object"}),
        ):
            with pytest.raises(PipelineExecutionError) as caught:
                call()
            assert caught.value.code == "stopped"
        assert data_store.get("app-x", "notes", "k") is None


class TestFailedLedgerRowCarriesPartialWrites:
    def test_a_timed_out_run_records_what_it_actually_wrote(self, tmp_path):
        app_store, data_store, run_store = _stores(tmp_path)
        pipeline = _pipeline()
        app = _app(pipeline)
        app_store.save(app)
        tracker = AppPipelineRunTracker(
            run_store=run_store,
            app_store=app_store,
            failure_handler=None,
            pipeline_runner=_runner(app_store, data_store),
            now_fn=lambda: NOW,
        )

        prior = _live_pipeline_threads()
        run, result = tracker.record_code_run(
            app, pipeline, params={"sleep": OVERRUN_SECONDS}, kind="scheduled", now=NOW
        )
        _join_pipeline_threads(prior)

        assert result is None
        assert run.status == "failed"
        assert "timeout" in (run.error or "")
        # The lie: this used to be {} while a document sat in the collection.
        assert run.docs_written == {"notes": 1}
        assert run_store.get(run.run_key).docs_written == {"notes": 1}


class TestTheErrorReachesTheModelActionably:
    def _handle(self, tmp_path) -> dict:
        app_store, data_store, _ = _stores(tmp_path)
        pipeline = _pipeline()
        app_store.save(_app(pipeline))
        tool = RunPipelineTool(
            session_id=SESSION_ID,
            app_store=app_store,
            runner=_runner(app_store, data_store),
        )
        prior = _live_pipeline_threads()
        speaker = asyncio.run(
            tool.handle(
                ActionStep(
                    tool_id="run_pipeline",
                    operation="run",
                    tool_input={"pipeline": "ingest", "params": {"sleep": OVERRUN_SECONDS}},
                )
            )
        )
        _join_pipeline_threads(prior)
        parsed = ast.literal_eval(speaker.content)
        assert isinstance(parsed, dict)
        return parsed["error"]

    def test_the_typed_timeout_code_survives_the_tool_boundary(self, tmp_path):
        # It used to arrive as code:"execution" — indistinguishable from a
        # NameError in the pipeline body, which needs the opposite response.
        assert self._handle(tmp_path)["code"] == "timeout"

    def test_the_message_names_the_query_that_settles_what_landed(self, tmp_path):
        message = self._handle(tmp_path)["message"]
        assert "app_data(operation='query'" in message
        assert "timeout_seconds" in message


class TestExecutionTimeoutIsDeclared:
    """The SessionTool declaration law: silence here is the loop's flat 120 s."""

    def _tool(self, tmp_path, *, timeout_seconds: int) -> RunPipelineTool:
        app_store, data_store, _ = _stores(tmp_path)
        app_store.save(_app(_pipeline(timeout_seconds=timeout_seconds)))
        return RunPipelineTool(
            session_id=SESSION_ID, app_store=app_store, runner=_runner(app_store, data_store)
        )

    def test_the_ceiling_is_derived_from_the_pipelines_own_watchdog(self, tmp_path):
        # 240 is the widest a pipeline may declare — the field is clamped inside
        # gunicorn's single-worker timeout, since a code pipeline holds the
        # request thread for its whole life.
        tool = self._tool(tmp_path, timeout_seconds=240)
        declared = tool.execution_timeout({"pipeline": "ingest"})
        assert declared is not None
        # Above the pipeline's own wall (never clips a legitimately long run) and
        # above the loop's undeclared 120 s fallback, which is the actual bug.
        assert declared > 240
        assert declared > 120

    def test_an_unresolvable_call_declares_the_widest_legal_ceiling(self, tmp_path):
        tool = self._tool(tmp_path, timeout_seconds=10)
        for tool_input in ("not-a-dict", {}, {"pipeline": "nope"}):
            declared = tool.execution_timeout(tool_input)
            assert declared is not None
            # DERIVED, never a restated literal. The fallback tracks
            # PipelineSpec.timeout_seconds' upper bound, and a test spelling
            # that number itself goes stale silently the moment the bound moves
            # — which is exactly what happened when it was clamped for the
            # gunicorn worker timeout.
            assert declared >= _MAX_PIPELINE_TIMEOUT_SECONDS
