"""A ``mode="code"`` pipeline's execution timeout must fit inside the gunicorn
worker's own timeout — WITHOUT tightening ``PipelineSpec.timeout_seconds``'s
parse bound.

The first cut of this fix DID tighten the field (``le=240``), and that was
wrong: the app store is APPEND-ONLY, and the deployed store already holds a
pipeline declaring ``timeout_seconds: 300``. A parse-time ``le=240`` makes
that manifest — and every ``app_versions`` snapshot holding it — fail to
VALIDATE, so a detail read, a rollback preview, or an ``include_archived``
listing 500s. Exactly the ``ensure_wakeable``/``ensure_unique_pipeline_names``
trap this package has hit before: validation of NEW data belongs at the trust
boundary it crosses (submit), never at the parse seam stored history
re-crosses under the contract it was written with.

The correct shape, four parts:

1. The field stays wide (``le=600``) so stored history keeps parsing.
2. ``AppSpec.ensure_pipeline_timeouts_fit()`` refuses a NEW/EDITED pipeline
   over :data:`~mewbo_api.apps.models.PIPELINE_TIMEOUT_CEILING_SECONDS` (240)
   at the SUBMIT boundary — covers every producer (a hand-built spec, a
   rollback replay), not just the one tool below.
3. A stored pipeline already over the ceiling is not stranded, and not
   honoured uncapped either: both ``RunPipelineTool.execution_timeout`` and
   ``AppPipelineRunner.execute``'s watchdog clamp it down to the ceiling and
   log a WARNING naming the app/pipeline — a silent narrowing is the fail-open
   shape this repo forbids.
4. ``submit_app.py``'s ``SubmitPipelineArgs.timeout_seconds`` carries the
   OPPOSITE bound (``le=PIPELINE_TIMEOUT_CEILING_SECONDS``) on purpose: it is
   a TOOL ARGUMENT, never parsed from storage, so refusing an over-ceiling
   value AT DEFINITION is the ordinary trust-boundary rule, not the
   append-only trap part 1 is dodging. Two spellings of one number, opposite
   constraints, both correct — never "harmonise" them.

Why 240 specifically: a ``mode="code"`` pipeline invoked over REST runs
SYNCHRONOUSLY on the request thread, and ``docker/Dockerfile.api``'s ``CMD``
runs a SINGLE gunicorn worker (``--workers 1 --timeout 300``) — with
``--workers 1`` a pipeline outliving the worker timeout doesn't fail alone,
it takes gunicorn's SIGKILL with it, dropping every other in-flight request.
240 plus ``RunPipelineTool``'s ``_EXECUTION_TIMEOUT_MARGIN_SECONDS`` (30, for
params validation/lint/cache-fingerprint/ledger-write work outside the
watchdog) is 270 — 30s of slack inside the worker's 300s timeout.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from loguru import logger as loguru_logger
from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import (
    PIPELINE_TIMEOUT_CEILING_SECONDS,
    AppFrontend,
    AppSpec,
    AppVersion,
    PipelineSpec,
    WorkspaceRef,
)
from mewbo_api.apps.pipeline_runner import AppPipelineRunner
from mewbo_api.apps.plugin.run_pipeline import _EXECUTION_TIMEOUT_MARGIN_SECONDS, RunPipelineTool
from mewbo_api.apps.plugin.submit_app import SubmitPipelineArgs
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore
from pydantic import ValidationError

from tests.apps.test_apps_lifecycle import FakeSessions

NOW = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)

#: The worker timeout gunicorn is started with — ``docker/Dockerfile.api``'s
#: ``CMD``: ``gunicorn --workers 1 --worker-class gthread --threads ${API_THREADS:-48}
#: --timeout 300``. Named here rather than imported, since no code module owns a
#: constant for a value that lives in a Dockerfile CMD.
_GUNICORN_WORKER_TIMEOUT_SECONDS = 300

#: The exact shape the deployed store holds today: an archived app whose
#: pipeline declares 300 — legal at parse time (le=600), over the execution
#: ceiling (240).
_OVER_CEILING_TIMEOUT = 300


@contextmanager
def _capture_loguru(level: str = "WARNING"):
    """Capture loguru records emitted inside the block.

    These modules log through loguru (``mewbo_core.common.get_logger``), which
    pytest's ``caplog`` never sees — a temporary sink is loguru's own documented
    way to assert on an emitted message (tests/CLAUDE.md).
    """
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda msg: messages.append(str(msg)), level=level)
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


def _pipeline(*, timeout_seconds: int, name: str = "p") -> PipelineSpec:
    return PipelineSpec(name=name, wake_prompt="w", on_demand=True, timeout_seconds=timeout_seconds)


class TestFieldStaysWideForTheAppendOnlyStore:
    """The field itself must never refuse what the store already holds."""

    def test_the_deployed_stores_own_value_still_parses(self):
        assert _pipeline(timeout_seconds=_OVER_CEILING_TIMEOUT).timeout_seconds == 300

    def test_600_still_parses(self):
        assert _pipeline(timeout_seconds=600).timeout_seconds == 600

    def test_601_still_refused(self):
        # The field's own absolute bound is untouched by this fix.
        with pytest.raises(ValidationError):
            _pipeline(timeout_seconds=601)

    def test_zero_still_refused(self):
        with pytest.raises(ValidationError):
            _pipeline(timeout_seconds=0)


class TestSubmitPipelineArgsIsTighterOnPurpose:
    """The tool-argument wire boundary — the OPPOSITE bound from the model field.

    ``SubmitPipelineArgs`` is never parsed from storage, so it is a pure trust
    boundary: refusing an over-ceiling value AT DEFINITION (not merely at
    ``AppLifecycle.submit``) is correct here, unlike ``PipelineSpec`` above.
    """

    @staticmethod
    def _args(*, timeout_seconds: int) -> SubmitPipelineArgs:
        return SubmitPipelineArgs(
            name="ingest", wake_prompt="w", mode="code",
            entrypoint="pipelines/ingest.py", timeout_seconds=timeout_seconds, on_demand=True,
        )

    def test_241_is_refused_at_definition(self):
        with pytest.raises(ValidationError):
            self._args(timeout_seconds=241)

    def test_the_ceiling_itself_is_accepted(self):
        assert self._args(
            timeout_seconds=PIPELINE_TIMEOUT_CEILING_SECONDS
        ).timeout_seconds == PIPELINE_TIMEOUT_CEILING_SECONDS

    def test_the_old_le_600_maximum_no_longer_parses_here(self):
        # The exact value PipelineSpec still accepts (le=600) must be refused
        # HERE — this is the assertion that fails if someone "harmonises" the
        # two bounds by widening this one to match.
        with pytest.raises(ValidationError):
            self._args(timeout_seconds=600)


class TestStoredSnapshotsKeepParsing:
    """The regression test for the mistake this file replaces.

    Builds the row the deployed store actually holds — an app whose pipeline
    declares 300 — through the REAL ``JsonAppStore``, both as the live manifest
    and as an ``app_versions`` snapshot, and reads both back. This is the
    direct sibling of ``test_apps_workspace_ref_validation.py``'s
    ``TestStoredSnapshotsKeepParsing`` — same shape, same reasoning, this
    field instead of ``workspace_ref.key``.
    """

    def test_a_stored_pipeline_over_the_ceiling_round_trips(self, tmp_path):
        store = JsonAppStore(root_dir=tmp_path / "apps")
        spec = AppSpec(
            app_id="app-89f9c9479143",
            title="Model compare",
            owner_session_id="owner-1",
            workspace_ref=WorkspaceRef(kind="own", key=""),
            frontend=AppFrontend(
                entrypoint="app.py", files={"app.py": "import streamlit as st\n"}
            ),
            pipelines=[
                _pipeline(name="ingest-model-snapshots", timeout_seconds=_OVER_CEILING_TIMEOUT)
            ],
            status="archived",
            created_at=NOW,
            updated_at=NOW,
        )
        store.save(spec)
        store.save_version(AppVersion(app_id=spec.app_id, version=1, spec=spec, author="builder"))

        read_back = store.get(spec.app_id)
        assert read_back is not None
        assert read_back.pipelines[0].timeout_seconds == _OVER_CEILING_TIMEOUT

        version = store.get_version(spec.app_id, 1)
        assert version is not None
        assert version.spec.pipelines[0].timeout_seconds == _OVER_CEILING_TIMEOUT

        # An include_archived listing must not choke on this row either — the
        # OTHER failure mode a parse-time floor causes (list comprehension over
        # every row, one bad row takes the whole call down).
        assert [a.app_id for a in store.list_apps(include_archived=True)] == [spec.app_id]


class TestSubmitRefusesOverTheCeiling:
    """The submit-boundary half: refuse NEW/EDITED data, never stored data."""

    def _make(self, tmp_path):
        app_store = JsonAppStore(root_dir=tmp_path / "apps")
        lifecycle = AppLifecycle(
            app_store=app_store,
            trigger_store=JsonTriggerStore(data_file=tmp_path / "triggers.json"),
            trigger_policy=TriggerPolicy(),
            sessions=FakeSessions(),
            now_fn=lambda: NOW,
        )
        return lifecycle, app_store

    def _draft(self, app_id: str, *, pipeline: PipelineSpec) -> AppSpec:
        return AppSpec(
            app_id=app_id,
            title="App",
            owner_session_id="builder-1",
            workspace_ref=WorkspaceRef(kind="own", key=""),
            frontend=AppFrontend(
                entrypoint="app.py", files={"app.py": "import streamlit as st\n"}
            ),
            pipelines=[pipeline],
            status="building",
            created_at=NOW,
            updated_at=NOW,
        )

    def test_241_is_refused_naming_the_pipeline(self, tmp_path):
        lifecycle, app_store = self._make(tmp_path)
        draft = self._draft(
            "app-a", pipeline=_pipeline(name="ingest", timeout_seconds=241)
        )
        with pytest.raises(ValueError) as excinfo:
            lifecycle.submit(draft, builder_session_id="builder-1")
        message = str(excinfo.value)
        assert "ingest" in message
        assert "241" in message
        assert str(PIPELINE_TIMEOUT_CEILING_SECONDS) in message
        # The refusal leaves no state, same ordering contract as every other
        # submit-boundary check.
        assert app_store.get("app-a") is None

    def test_240_is_accepted(self, tmp_path):
        lifecycle, app_store = self._make(tmp_path)
        draft = self._draft(
            "app-b", pipeline=_pipeline(name="ingest", timeout_seconds=240)
        )
        live = lifecycle.submit(draft, builder_session_id="builder-1")
        assert live.status == "live"
        assert app_store.get("app-b") is not None


class TestExecutionTimeoutIsClamped:
    """``RunPipelineTool.execution_timeout`` must never declare an outer ceiling
    a stored-but-over-limit pipeline could actually reach.
    """

    def _tool(self, tmp_path, *, timeout_seconds: int) -> tuple[RunPipelineTool, str]:
        app_store = JsonAppStore(root_dir=tmp_path / "apps")
        data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
        app = AppSpec(
            app_id="app-clamp",
            title="App",
            owner_session_id="owner",
            maintainer_session_id="maint-1",
            workspace_ref=WorkspaceRef(kind="own", key=""),
            frontend=AppFrontend(
                entrypoint="app.py", files={"app.py": "import streamlit as st\n"}
            ),
            pipelines=[_pipeline(name="ingest", timeout_seconds=timeout_seconds)],
            status="live",
        )
        app_store.save(app)
        runner = AppPipelineRunner(
            app_store=app_store, app_data=data_store, workspace_resolver=lambda _a: None,
            clock=lambda: NOW,
        )
        tool = RunPipelineTool(session_id="maint-1", app_store=app_store, runner=runner)
        return tool, app.app_id

    def test_a_stored_300_pipeline_is_clamped_to_the_ceiling_plus_margin(self, tmp_path):
        tool, _ = self._tool(tmp_path, timeout_seconds=_OVER_CEILING_TIMEOUT)
        with _capture_loguru() as messages:
            declared = tool.execution_timeout({"pipeline": "ingest"})
        # NOT 300 + margin (330) — proves the clamp actually changed the value,
        # not merely that SOME number came back.
        assert declared == PIPELINE_TIMEOUT_CEILING_SECONDS + _EXECUTION_TIMEOUT_MARGIN_SECONDS
        assert any(
            "app-clamp" in m and "ingest" in m and "300" in m and "240" in m for m in messages
        )

    def test_a_240_pipeline_is_not_clamped_and_logs_nothing(self, tmp_path):
        tool, _ = self._tool(tmp_path, timeout_seconds=PIPELINE_TIMEOUT_CEILING_SECONDS)
        with _capture_loguru() as messages:
            declared = tool.execution_timeout({"pipeline": "ingest"})
        assert declared == PIPELINE_TIMEOUT_CEILING_SECONDS + _EXECUTION_TIMEOUT_MARGIN_SECONDS
        assert messages == []


class TestWatchdogClampsAndLogs:
    """``AppPipelineRunner.execute`` must clamp the actual watchdog too — the
    ``execution_timeout`` hook only declares the LOOP's outer ceiling; without
    this, a stored-over-ceiling pipeline would still be handed to the watchdog
    at its full declared value.
    """

    def test_a_stored_300_pipeline_executes_under_a_clamped_watchdog_and_logs(self, tmp_path):
        app_store = JsonAppStore(root_dir=tmp_path / "apps")
        data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
        pipeline = PipelineSpec(
            name="ingest",
            wake_prompt="w",
            mode="code",
            entrypoint="pipelines/ingest.py",
            on_demand=True,
            timeout_seconds=_OVER_CEILING_TIMEOUT,
        )
        app = AppSpec(
            app_id="app-watchdog",
            title="App",
            owner_session_id="owner",
            workspace_ref=WorkspaceRef(kind="own", key=""),
            frontend=AppFrontend(
                files={
                    "app.py": "import streamlit as st",
                    "pipelines/ingest.py": "def run(params, ctx):\n    return 1\n",
                }
            ),
            pipelines=[pipeline],
            status="live",
        )
        runner = AppPipelineRunner(
            app_store=app_store, app_data=data_store, workspace_resolver=lambda _a: None,
            clock=lambda: NOW,
        )
        with _capture_loguru() as messages:
            result = runner.execute(app, pipeline, {}, now=NOW)
        assert result.output == 1
        assert any(
            "app-watchdog" in m and "ingest" in m and "300" in m and "240" in m for m in messages
        )


class TestWidestCeilingFitsInsideTheWorkerTimeout:
    """The invariant this whole clamp exists for.

    A future author raising ``PIPELINE_TIMEOUT_CEILING_SECONDS`` or
    ``_EXECUTION_TIMEOUT_MARGIN_SECONDS`` breaks this test before they ever
    reach production. Deliberately does NOT go through ``PipelineSpec`` —
    the field's own bound (600) is no longer the number that matters; the
    ENFORCED ceiling is :data:`PIPELINE_TIMEOUT_CEILING_SECONDS`.
    """

    def test_the_ceiling_plus_margin_fits_inside_300s(self):
        worst_case_outer_ceiling = (
            PIPELINE_TIMEOUT_CEILING_SECONDS + _EXECUTION_TIMEOUT_MARGIN_SECONDS
        )
        assert worst_case_outer_ceiling < _GUNICORN_WORKER_TIMEOUT_SECONDS
