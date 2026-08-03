"""The scoped refresh's act-phase launcher seam + its api-side implementation."""
from __future__ import annotations

import pytest
from mewbo_api.wiki.act_launcher_impl import SessionActLauncher
from mewbo_graph.plugins.wiki.act_launcher import ActLauncher, ActOutcome


@pytest.fixture(autouse=True)
def _reset_launcher():
    """Isolate this module from the process-wide launcher, in BOTH directions.

    ``ActLauncher._impl`` is a ``ClassVar``, so it is shared by every test in
    the interpreter and ``init_wiki`` registers a real ``SessionActLauncher``
    into it. Clearing on setup keeps an earlier api-initialising test from
    turning an "act phase is absent" assertion here into a real session start;
    RESTORING on teardown (rather than clearing again) keeps this module from
    doing the same damage in reverse to whatever runs next. Both halves are
    load-bearing — each direction passes alone and fails only under a full-suite
    ordering.
    """
    previous = ActLauncher._impl
    ActLauncher.reset()
    yield
    ActLauncher.register(previous)


class _RecordingLauncher:
    """A minimal ``ActLauncherImpl`` that records what it was asked to do."""

    def __init__(self, session_id: str | None = "s1"):
        self.session_id = session_id
        self.started: list[dict] = []
        self.waited: list[tuple[str, float | None]] = []

    def start(self, *, slug, job_id, page_ids, submission=None):
        self.started.append(
            {"slug": slug, "job_id": job_id, "page_ids": list(page_ids), "submission": submission}
        )
        return self.session_id

    def wait(self, session_id, *, timeout=None):
        self.waited.append((session_id, timeout))
        return ActOutcome(session_id=session_id, status="completed")


# ── the seam ────────────────────────────────────────────────────────────────


def test_no_launcher_degrades_to_absent_never_raises():
    # Absence is None on BOTH calls: the refresh finalizes exactly as it did
    # before an act tier existed.
    assert ActLauncher.available() is False
    assert ActLauncher.start(slug="h/o/r", job_id="j1", page_ids=["p1"]) is None
    assert ActLauncher.wait("s1") is None


def test_register_then_delegates_and_reset_restores_absence():
    impl = _RecordingLauncher()
    ActLauncher.register(impl)

    assert ActLauncher.available() is True
    assert ActLauncher.start(slug="h/o/r", job_id="j1", page_ids=["p1", "p2"]) == "s1"
    assert impl.started == [
        {"slug": "h/o/r", "job_id": "j1", "page_ids": ["p1", "p2"], "submission": None}
    ]
    outcome = ActLauncher.wait("s1", timeout=5.0)
    assert outcome is not None and outcome.ok
    assert impl.waited == [("s1", 5.0)]

    ActLauncher.reset()
    assert ActLauncher.available() is False
    assert ActLauncher.start(slug="h/o/r", job_id="j1", page_ids=[]) is None


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        ("completed", True),
        ("failed", False),
        ("unmet_goal", False),
        ("timeout", False),
        ("idle", False),
    ],
)
def test_outcome_ok_is_completed_only(status, ok):
    assert ActOutcome(session_id="s1", status=status).ok is ok


# ── the api-side impl ───────────────────────────────────────────────────────


class _FakeRuntime:
    def __init__(self, statuses, *, wiki_store=object(), running=False):
        self.wiki_store = wiki_store
        self._statuses = list(statuses)
        self._running = running
        self.summaries = 0

    def summarize_session(self, session_id):
        self.summaries += 1
        status = self._statuses.pop(0) if self._statuses else "completed"
        return {"status": status, "done_reason": "boom" if status == "failed" else None}

    def is_running(self, session_id):
        running, self._running = self._running, False
        return running


def _launcher(runtime, **kw):
    clock = {"t": 0.0}

    def _sleep(seconds):
        clock["t"] += seconds

    return SessionActLauncher(
        runtime=runtime,
        sleep=_sleep,
        monotonic=lambda: clock["t"],
        **kw,
    )


def test_start_delegates_to_the_jobs_seam(monkeypatch):
    from mewbo_api.wiki import jobs

    seen: dict = {}

    def _fake(*, store, runtime, job_id, slug, page_ids, submission=None, hook_manager=None):
        seen.update(
            {
                "store": store,
                "job_id": job_id,
                "slug": slug,
                "page_ids": page_ids,
                "submission": submission,
                "hook_manager": hook_manager,
            }
        )
        return "sess-1"

    monkeypatch.setattr(jobs, "_start_refresh_act_session", _fake, raising=False)
    store = object()
    runtime = _FakeRuntime([], wiki_store=store)

    launcher = _launcher(runtime, hook_manager="hm")
    assert launcher.start(slug="h/o/r", job_id="j1", page_ids=("p1", "p2")) == "sess-1"
    assert seen["store"] is store
    assert seen["page_ids"] == ["p1", "p2"]
    assert seen["hook_manager"] == "hm"
    # start does NOT block — the caller records the id before waiting.
    assert runtime.summaries == 0


def test_start_reports_a_refused_start_as_absence(monkeypatch):
    from mewbo_api.wiki import jobs

    monkeypatch.setattr(
        jobs, "_start_refresh_act_session", lambda **_kw: "", raising=False
    )
    launcher = _launcher(_FakeRuntime([]))
    assert launcher.start(slug="h/o/r", job_id="j1", page_ids=["p1"]) is None


def test_start_degrades_when_the_jobs_seam_is_absent(monkeypatch):
    from mewbo_api.wiki import jobs

    monkeypatch.delattr(jobs, "_start_refresh_act_session", raising=False)
    launcher = _launcher(_FakeRuntime([]))
    assert launcher.start(slug="h/o/r", job_id="j1", page_ids=["p1"]) is None


def test_start_degrades_when_no_wiki_store_is_bound():
    launcher = _launcher(_FakeRuntime([], wiki_store=None))
    assert launcher.start(slug="h/o/r", job_id="j1", page_ids=["p1"]) is None


def test_wait_polls_until_the_session_settles():
    runtime = _FakeRuntime(["running", "running", "completed"])
    outcome = _launcher(runtime).wait("s1")
    assert outcome.ok and outcome.status == "completed" and outcome.error is None
    assert runtime.summaries == 3


def test_wait_reports_a_failure_with_its_reason():
    outcome = _launcher(_FakeRuntime(["failed"])).wait("s1")
    assert outcome.ok is False
    assert outcome.status == "failed"
    assert outcome.error == "boom"


def test_wait_treats_a_live_run_as_in_flight_even_when_the_status_lags():
    # ``idle`` with a live run is the window right after ``start_async``: the
    # transcript has no run yet, so status alone would settle the wait early.
    runtime = _FakeRuntime(["idle", "completed"], running=True)
    outcome = _launcher(runtime).wait("s1")
    assert outcome.status == "completed"
    assert runtime.summaries == 2


def test_the_runner_call_shape_reaches_the_real_impl(monkeypatch):
    """Drive the seam exactly as ``ScopedRefreshRunner`` does, no mocks between.

    The runner's suite fakes this launcher and this suite fakes the runner, so a
    keyword renamed on one side would leave both green. This test closes that by
    registering the REAL impl and calling through the REAL classmethods with the
    runner's own argument spelling.
    """
    from mewbo_api.wiki import jobs

    monkeypatch.setattr(
        jobs, "_start_refresh_act_session", lambda **_kw: "sess-1", raising=False
    )
    ActLauncher.register(_launcher(_FakeRuntime(["completed"])))

    session_id = ActLauncher.start(
        job_id="j1", slug="h/o/r", page_ids=["p1"], submission=None
    )
    assert session_id == "sess-1"
    outcome = ActLauncher.wait(session_id)
    assert outcome is not None and outcome.ok


def test_wait_gives_up_at_the_deadline_with_a_distinct_status():
    # "we stopped watching" must not read as any status the session can carry.
    runtime = _FakeRuntime(["running"] * 50)
    outcome = _launcher(runtime, timeout_seconds=4.0, poll_interval=1.0).wait("s1")
    assert outcome.status == "timeout"
    assert outcome.ok is False
    assert "did not settle" in (outcome.error or "")
