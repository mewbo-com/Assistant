"""Resuming a scoped refresh that died with an act stage owed."""
from __future__ import annotations

import pytest
from mewbo_api.wiki.resume import WikiResume
from mewbo_graph.wiki.types import IndexingJob, RefreshDecision

_SUBMISSION = {
    "repoUrl": "https://git.example.com/o/r",
    "slug": "h/o/r",
    "platform": "gitea",
    "depth": "comprehensive",
    "language": "en",
    "model": "m1",
    "filterMode": "exclude",
    "dirs": [],
    "files": [],
}


class _Store:
    """The store surface the scoped resume branch touches."""

    def __init__(self, act_plan=None, *, raises=False):
        self.act_plan = act_plan
        self.raises = raises
        self.job = IndexingJob(
            job_id="job-1",
            slug="h/o/r",
            status="interrupted",
            scanned_count=0,
            total_count=0,
            current_file=None,
            refresh_decision=RefreshDecision(path="scoped", reason=None),
        )
        self.events: list[dict] = []
        self.updates: list[dict] = []

    def get_job(self, job_id):
        return self.job

    def get_job_session(self, job_id):
        return ""

    def get_job_submission(self, job_id):
        return dict(_SUBMISSION)

    def get_act_plan(self, job_id):
        if self.raises:
            raise RuntimeError("mongo hiccup")
        return self.act_plan

    def update_job(self, job_id, **fields):
        self.updates.append(fields)

    def append_job_event(self, job_id, event):
        self.events.append(event)

    def reset_recovery_attempts(self, slug):
        pass


class _Runtime:
    def is_running(self, session_id):
        return False


@pytest.fixture
def redriven(monkeypatch):
    """Record every scoped re-drive instead of starting a real runner."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "mewbo_api.wiki.resume._start_scoped_refresh",
        lambda **kw: calls.append(kw),
    )
    return calls


def _resume(store, redriven):
    return WikiResume.resume(store, _Runtime(), "job-1")


# ── reading the sidecar ─────────────────────────────────────────────────────


def test_owed_pages_come_off_an_act_stage_record():
    store = _Store({"stage": "act", "page_ids": ["p1", "p2"], "commit_sha": "abc"})
    assert WikiResume._owed_act_pages(store, "job-1") == ["p1", "p2"]


def test_a_finalize_stage_record_owes_nothing():
    # Stage 2 is done (or had nothing to do) — only publication remained.
    store = _Store({"stage": "finalize", "page_ids": ["p1"], "commit_sha": "abc"})
    assert WikiResume._owed_act_pages(store, "job-1") == []


@pytest.mark.parametrize(
    "record",
    [None, {}, {"stage": "act"}, {"stage": "act", "page_ids": "p1"}],
)
def test_a_missing_or_malformed_record_owes_nothing(record):
    assert WikiResume._owed_act_pages(_Store(record), "job-1") == []


def test_a_store_hiccup_degrades_to_the_whole_run_redrive():
    # Not silently: the read failure is logged (loguru, so not assertable via
    # caplog — see tests/CLAUDE.md). What IS assertable is that it degrades to
    # the pre-sidecar behaviour rather than claiming nothing was owed.
    store = _Store(raises=True)
    assert WikiResume._owed_act_pages(store, "job-1") == []


# ── what the resume actually does ───────────────────────────────────────────


def test_a_scoped_resume_with_nothing_owed_reads_as_it_always_did(redriven):
    store = _Store({"stage": "finalize", "page_ids": [], "commit_sha": "abc"})
    result = _resume(store, redriven)

    assert result["status"] == "scanning"
    assert len(redriven) == 1
    (event,) = store.events
    assert event["level"] == "info"
    assert event["text"] == "Resuming scoped refresh (re-clone + re-diff, no LLM agent)"


def test_a_scoped_resume_with_an_owed_work_list_says_it_will_recover_them(redriven):
    # The runner recovers the same work-list from the act sidecar and
    # regenerates these pages within this run — that has to be visible on the
    # job log, which is what the indexing view and the landing card render.
    store = _Store({"stage": "act", "page_ids": ["p1", "p2"], "commit_sha": "abc"})
    _resume(store, redriven)

    (event,) = store.events
    assert event["level"] == "warning"
    assert "2 page(s) were still owed" in event["text"]
    assert "p1, p2" in event["text"]
    assert "recovered from the act sidecar and are being regenerated" in event["text"]


def test_the_work_list_preview_is_bounded(redriven):
    store = _Store(
        {"stage": "act", "page_ids": [f"p{i}" for i in range(20)], "commit_sha": "abc"}
    )
    _resume(store, redriven)

    text = store.events[0]["text"]
    assert "20 page(s) were still owed" in text
    assert "…" in text
    assert "p9" not in text  # only the first eight are named


def test_an_in_flight_act_session_refuses_the_resume(redriven):
    """The existing guard covers stage 2 because the act session binds to the job."""

    class _LiveStore(_Store):
        def get_job_session(self, job_id):
            return "sess-act"

    class _LiveRuntime:
        def is_running(self, session_id):
            return session_id == "sess-act"

    store = _LiveStore({"stage": "act", "page_ids": ["p1"], "commit_sha": "abc"})
    with pytest.raises(RuntimeError, match="already running"):
        WikiResume.resume(store, _LiveRuntime(), "job-1")
    assert redriven == []
    assert store.updates == []  # refused BEFORE any mutation
