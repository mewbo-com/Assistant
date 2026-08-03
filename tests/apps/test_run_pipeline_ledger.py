"""The ``run_pipeline`` SessionTool records its run on the provenance ledger.

The defect these pin, from a real session: the model invoked a pipeline, the call
errored with a timeout, and the model then asked "did my work land?" — reading
``last_run_status: 'succeeded'`` off ``get_app``. Arithmetic on the freshness
numbers in the same transcript refuted that reading: the ``succeeded`` it read
described a run that had finished ~7 minutes BEFORE the invoke. There was no row
for the invoke at all, because the tool called the runner's id-keyed adapter
directly and that adapter writes no ledger row.

So the contract here is the LEDGER ADVANCING, never the tool's return shape alone:
a non-dry invoke writes exactly one ``kind="on_request"`` row (success or failure),
and its ``run_key`` reaches the model on BOTH envelopes so the row can be read back
instead of a stale manifest field being taken for this run's outcome.

Real JSON stores (the ledger math IS the contract), a real ``AppPipelineRunner``
over a real two-file workspace, an injected NOW, no LLM and no network.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone

import pytest
from mewbo_api.apps.models import (
    AppFrontend,
    AppPolicies,
    AppSpec,
    CollectionSpec,
    PipelineRun,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import AppPipelineRunner, PipelineExecutionError
from mewbo_api.apps.pipeline_tracker import AppPipelineRunTracker
from mewbo_api.apps.plugin import runtime as plugin_runtime
from mewbo_api.apps.plugin.run_pipeline import RunPipelineTool
from mewbo_api.apps.store import (
    JsonAppDataStore,
    JsonAppStore,
    JsonPipelineRunStore,
    new_run_key,
)
from mewbo_core.classes import ActionStep

SESSION_ID = "maint-1"
# The run that already existed before the model ever invoked anything — the row
# whose "succeeded" the model misread as its own.
EARLIER = datetime(2026, 7, 18, 17, 25, 36, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 18, 17, 33, 52, tzinfo=timezone.utc)

_OPEN_SCHEMA: dict[str, object] = {"type": "object"}
_GLOB_PIPELINE = (
    "def run(params, ctx):\n"
    "    for path in ctx.glob('*.md'):\n"
    "        ctx.collection('notes').upsert(path, {'path': path})\n"
    "    return {'ok': True}\n"
)


@pytest.fixture
def workspace(tmp_path):
    """A two-file workspace the pipeline's ``ctx.glob``/``read_file`` see."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha", encoding="utf-8")
    (ws / "b.md").write_text("bravo", encoding="utf-8")
    return ws


def _pipeline(**overrides) -> PipelineSpec:
    fields: dict[str, object] = {
        "name": "p",
        "wake_prompt": "wake",
        "mode": "code",
        "entrypoint": "pipelines/p.py",
        "on_demand": True,
        "cache_ttl_seconds": 0,
    }
    fields.update(overrides)
    return PipelineSpec(**fields)


def _app(pipeline: PipelineSpec, *, source: str = _GLOB_PIPELINE) -> AppSpec:
    return AppSpec(
        app_id="app-x",
        title="App",
        owner_session_id="owner",
        workspace_ref=WorkspaceRef(kind="own", key="k"),
        frontend=AppFrontend(files={"app.py": "import streamlit as st", "pipelines/p.py": source}),
        collections=[CollectionSpec(name="notes", json_schema=_OPEN_SCHEMA)],
        pipelines=[pipeline],
        policies=AppPolicies(),
        # The tool resolves its app by SCOPE alone — this is what binds the session.
        maintainer_session_id=SESSION_ID,
        status="live",
    )


class _RecordingFailureHandler:
    """Records every ``on_pipeline_failure`` dispatch — the manual-fire law's witness."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def handle_pipeline_failure(self, app, issue) -> None:
        self.calls.append((app.app_id, issue))


class _RaisingRunner:
    """A runner whose ``execute`` always raises — the timeout shape from the report.

    Wraps a REAL runner so ``params_hash`` (which ``record_code_run`` stamps on the
    row) stays the genuine implementation rather than a stub that could drift.
    """

    def __init__(self, inner: AppPipelineRunner, exc: PipelineExecutionError) -> None:
        self._inner = inner
        self._exc = exc

    def execute(self, *_args, **_kwargs):
        raise self._exc

    def run_pipeline(self, *_args, **_kwargs):
        raise self._exc

    def params_hash(self, params):
        return self._inner.params_hash(params)


def _wire(tmp_path, workspace, *, pipeline=None, handler=None, runner_exc=None):
    """Real stores + a real runner + the tracker; returns (tool, run_store, handler)."""
    root = tmp_path / "apps"
    app_store = JsonAppStore(root_dir=root)
    data_store = JsonAppDataStore(root_dir=root)
    run_store = JsonPipelineRunStore(root_dir=root)
    spec = pipeline or _pipeline()
    app_store.save(_app(spec))
    runner: object = AppPipelineRunner(
        app_store=app_store,
        app_data=data_store,
        workspace_resolver=lambda _a: str(workspace),
        clock=lambda: NOW,
    )
    if runner_exc is not None:
        runner = _RaisingRunner(runner, runner_exc)
    tracker = AppPipelineRunTracker(
        run_store=run_store,
        app_store=app_store,
        failure_handler=handler,
        pipeline_runner=runner,
        now_fn=lambda: NOW,
    )
    tool = RunPipelineTool(
        session_id=SESSION_ID, app_store=app_store, runner=runner, ledger=tracker
    )
    return tool, run_store, app_store


def _invoke(tool: RunPipelineTool, **tool_input) -> dict:
    step = ActionStep(
        tool_id="run_pipeline", operation="execute", tool_input={"pipeline": "p", **tool_input}
    )
    speaker = asyncio.run(tool.handle(step))
    payload = ast.literal_eval(speaker.content)
    assert isinstance(payload, dict)
    return payload


def _seed_old_succeeded_run(run_store) -> None:
    """The pre-existing green row the model read at the START of its session."""
    run = PipelineRun.open(
        run_key=new_run_key(),
        app_id="app-x",
        pipeline_name="p",
        now=EARLIER,
        kind="scheduled",
    )
    run.record_write("notes", 2)
    run.close(now=EARLIER, status="succeeded", cache="miss")
    run_store.save(run)


class TestLedgerAdvances:
    def test_a_successful_invoke_writes_a_new_row_dated_now(self, tmp_path, workspace):
        # THE REGRESSION, stated as the issue states it: an OLD succeeded row is the
        # newest thing on the ledger; the model invokes the pipeline; the ledger must
        # ADVANCE. Before the fix the newest row is still the old one, so the model
        # reading "last run succeeded" is reading a run from seven minutes earlier.
        tool, run_store, _ = _wire(tmp_path, workspace)
        _seed_old_succeeded_run(run_store)

        payload = _invoke(tool)

        rows = run_store.list_runs("app-x")
        assert len(rows) == 2
        newest = max(rows, key=lambda r: r.started_at)
        assert newest.started_at == NOW  # the ledger describes THIS invoke, not the old run
        assert newest.status == "succeeded"
        assert newest.kind == "on_request"  # a model invoke is not a schedule
        assert newest.docs_written == {"notes": 2}
        assert payload["run_key"] == newest.run_key

    def test_run_key_on_the_success_envelope_resolves_to_the_row(self, tmp_path, workspace):
        tool, run_store, _ = _wire(tmp_path, workspace)

        payload = _invoke(tool)

        assert payload["run_key"]
        matched = [r for r in run_store.list_runs("app-x") if r.run_key == payload["run_key"]]
        assert len(matched) == 1
        assert matched[0].status == "succeeded"


class TestFailingInvokeIsObservable:
    """The exact shape from the report: a timeout with partial writes already landed."""

    @staticmethod
    def _timeout() -> PipelineExecutionError:
        return PipelineExecutionError(
            "timeout",
            "pipeline exceeded its 30s deadline",
            docs_written={"notes": 3},
        )

    def test_a_timed_out_invoke_ledgers_a_failed_row_with_its_partial_writes(
        self, tmp_path, workspace
    ):
        tool, run_store, _ = _wire(tmp_path, workspace, runner_exc=self._timeout())

        payload = _invoke(tool)

        rows = run_store.list_runs("app-x")
        assert len(rows) == 1
        run = rows[0]
        assert run.status == "failed"
        assert run.started_at == NOW
        # The documents written BEFORE the deadline are recorded, not lost.
        assert run.docs_written == {"notes": 3}
        # ...and the error envelope names the row, so the model can read it back.
        assert payload["error"]["code"] == "timeout"
        assert payload["error"]["run_key"] == run.run_key
        assert run.run_key in payload["error"]["message"]

    def test_a_failed_invoke_never_dispatches_on_pipeline_failure(self, tmp_path, workspace):
        # The manual-fire law: a model manually invoking a broken pipeline must never
        # auto-repair or auto-pause the app. Only an autonomous SCHEDULED fire does.
        handler = _RecordingFailureHandler()
        tool, _, _ = _wire(
            tmp_path, workspace, handler=handler, runner_exc=self._timeout()
        )

        _invoke(tool)

        assert handler.calls == []

    def test_the_typed_failure_bucket_survives_the_trip_through_the_row(
        self, tmp_path, workspace
    ):
        # record_code_run reports failure by returning result=None, so the code has to
        # be recovered from run.error rather than an exception — it must still be the
        # TYPED bucket the model can act on, never a flat "execution".
        tool, _, _ = _wire(
            tmp_path,
            workspace,
            runner_exc=PipelineExecutionError("params", "n is required"),
        )

        payload = _invoke(tool)

        assert payload["error"]["code"] == "params"


class TestDryRunAndUnwired:
    def test_dry_run_ledgers_nothing_and_reports_no_run_key(self, tmp_path, workspace):
        # A dry run performs no durable write, so a row for it would claim a refresh
        # that never happened.
        tool, run_store, _ = _wire(tmp_path, workspace)

        payload = _invoke(tool, dry_run=True)

        assert run_store.list_runs("app-x") == []
        assert payload["run_key"] is None
        assert payload["dry_run"] is True

    def test_dry_run_still_exercises_the_pipeline(self, tmp_path, workspace):
        tool, _, _ = _wire(tmp_path, workspace)

        payload = _invoke(tool, dry_run=True)

        assert "error" not in payload
        assert payload["docs_written"] == {"notes": 2}  # counted, never written

    def test_an_unwired_ledger_still_executes_and_reports_no_run_key(
        self, tmp_path, workspace, monkeypatch
    ):
        # A deployment that has not wired the tracker keeps working exactly as before.
        # The push seam is a process global that ``backend.py`` registers at import, so
        # clear it rather than relying on this test's import order — and note the tool
        # would not consult it here anyway (see the pairing test below).
        monkeypatch.setattr(plugin_runtime, "_PIPELINE_LEDGER", None)
        root = tmp_path / "apps"
        app_store = JsonAppStore(root_dir=root)
        data_store = JsonAppDataStore(root_dir=root)
        run_store = JsonPipelineRunStore(root_dir=root)
        app_store.save(_app(_pipeline()))
        runner = AppPipelineRunner(
            app_store=app_store,
            app_data=data_store,
            workspace_resolver=lambda _a: str(workspace),
            clock=lambda: NOW,
        )
        tool = RunPipelineTool(
            session_id=SESSION_ID, app_store=app_store, runner=runner, ledger=None
        )

        payload = _invoke(tool)

        assert "error" not in payload
        assert payload["run_key"] is None
        assert run_store.list_runs("app-x") == []

    def test_an_injected_runner_is_never_bypassed_by_a_globally_wired_ledger(
        self, tmp_path, workspace, monkeypatch
    ):
        # The ledger executes through the runner IT holds, so consulting the global
        # ledger seam while a runner was injected would silently run somebody else's
        # runner. A caller that injected one gets exactly that one.
        root = tmp_path / "apps"
        app_store = JsonAppStore(root_dir=root)
        run_store = JsonPipelineRunStore(root_dir=root)
        app_store.save(_app(_pipeline()))
        # A globally-registered ledger over a DIFFERENT (empty) store.
        other_runs = JsonPipelineRunStore(root_dir=tmp_path / "other")
        # monkeypatch, never ``register_pipeline_ledger`` — the seam is a process
        # global with no unregister, so a bare register leaks into every later test.
        monkeypatch.setattr(
            plugin_runtime,
            "_PIPELINE_LEDGER",
            AppPipelineRunTracker(
                run_store=other_runs,
                app_store=app_store,
                pipeline_runner=AppPipelineRunner(
                    app_store=app_store,
                    app_data=JsonAppDataStore(root_dir=root),
                    workspace_resolver=lambda _a: str(workspace),
                    clock=lambda: NOW,
                ),
                now_fn=lambda: NOW,
            ),
        )
        injected = _RaisingRunner(
            AppPipelineRunner(
                app_store=app_store,
                app_data=JsonAppDataStore(root_dir=root),
                workspace_resolver=lambda _a: str(workspace),
                clock=lambda: NOW,
            ),
            PipelineExecutionError("runtime", "the INJECTED runner ran"),
        )
        tool = RunPipelineTool(session_id=SESSION_ID, app_store=app_store, runner=injected)

        payload = _invoke(tool)

        assert "the INJECTED runner ran" in payload["error"]["message"]
        assert other_runs.list_runs("app-x") == []  # the global ledger was not consulted
        assert run_store.list_runs("app-x") == []
